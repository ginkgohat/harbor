"""Regression guard for T-003: user-facing CJK text must live in the STR table.

Any CJK character found in index.html *outside* the `const STR = {...}` block
is a hardcoded string that won't switch language — fail the suite so it gets
moved into STR.zh / STR.en.

Exception: the language-toggle buttons (`data-lang="zh"` → 中) are inherently
language-neutral labels, not translatable UI copy.
"""

import re
from pathlib import Path

INDEX_HTML = (
    Path(__file__).resolve().parent.parent / "src" / "harbor" / "static" / "index.html"
)

CJK_RE = re.compile(r"[⺀-鿿豈-﫿　-〿＀-￯]")


def _strip_str_table(source):
    """Return *source* with the `const STR = {...}` object literal removed."""
    start = source.index("const STR = {")
    brace_at = source.index("{", start)
    depth = 0
    for i in range(brace_at, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[:start] + source[i + 1 :]
    raise AssertionError("unbalanced braces while scanning the STR table")


def test_no_cjk_outside_str_table():
    source = INDEX_HTML.read_text(encoding="utf-8")
    remainder = _strip_str_table(source)
    hits = [
        (lineno, line.strip())
        for lineno, line in enumerate(remainder.splitlines(), 1)
        if CJK_RE.search(line) and "data-lang" not in line
    ]
    assert not hits, (
        "hardcoded CJK outside the STR table (move it into STR.zh/en):\n"
        + "\n".join(f"  L{lineno}: {line[:100]}" for lineno, line in hits)
    )


def _lang_block(source, lang):
    m = re.search(rf"{lang}: \{{(.*?)\n  \}},\s*\n", source, re.DOTALL)
    assert m, f"STR.{lang} block not found"
    return m.group(1)


def _lang_keys(source, lang):
    """Return the set of STR keys for one language.

    String / template-literal values are stripped first so ``Word: `` phrases
    inside English copy (e.g. ``"Theme: system"``) are not mistaken for keys.
    The block may be formatted one-key-per-line or many-on-a-line, so matches
    do not rely on line starts.
    """
    block = _lang_block(source, lang)
    block = re.sub(r"`[^`]*`", "", block)
    block = re.sub(r'"[^"]*"', "", block)
    block = re.sub(r"'[^']*'", "", block)
    return set(re.findall(r"(?<![\w])([A-Za-z_]\w*)\s*:", block))


def test_str_tables_have_identical_keys():
    """zh and en must expose the same key set — a missing key renders as
    `undefined` in one language."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    zh, en = _lang_keys(source, "zh"), _lang_keys(source, "en")
    assert zh == en, (
        f"STR key mismatch — zh-only: {sorted(zh - en)}, en-only: {sorted(en - zh)}"
    )


UTILS_JS = (
    Path(__file__).resolve().parent.parent / "src" / "harbor" / "static" / "harbor-utils.js"
)


def test_str_tables_in_sync_across_files():
    """The STR table duplicated in harbor-utils.js must match index.html's.

    The two files each carry a copy kept "in sync manually".  This guard catches
    drift — a UI string added only to one file would behave differently in the
    browser (index.html) vs. tests (harbor-utils.js).  Keys are compared in both
    the zh and en tables.
    """
    idx = INDEX_HTML.read_text(encoding="utf-8")
    utils = UTILS_JS.read_text(encoding="utf-8")
    for lang in ("zh", "en"):
        index_keys = _lang_keys(idx, lang)
        utils_keys = _lang_keys(utils, lang)
        assert index_keys == utils_keys, (
            f"STR.{lang} drift between index.html and harbor-utils.js —\n"
            f"  index.html-only: {sorted(index_keys - utils_keys)}\n"
            f"  harbor-utils.js-only: {sorted(utils_keys - index_keys)}"
        )
