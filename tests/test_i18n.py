"""Regression guard for T-003: user-facing CJK text must live in the STR table.

Any CJK character found in index.html *outside* the `const STR = {...}` block
is a hardcoded string that won't switch language — fail the suite so it gets
moved into STR.zh / STR.en.

Exception: the language-toggle buttons (`data-lang="zh"` → 中) are inherently
language-neutral labels, not translatable UI copy.
"""

import re
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parent.parent / "src" / "harbor" / "static" / "index.html"

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
    assert not hits, "hardcoded CJK outside the STR table (move it into STR.zh/en):\n" + "\n".join(
        f"  L{lineno}: {line[:100]}" for lineno, line in hits
    )


def test_str_tables_have_identical_keys():
    """zh and en must expose the same key set — a missing key renders as
    `undefined` in one language."""
    source = INDEX_HTML.read_text(encoding="utf-8")

    def keys(lang):
        m = re.search(rf"{lang}: \{{(.*?)\n  \}},", source, re.DOTALL)
        assert m, f"STR.{lang} block not found"
        return set(re.findall(r"^ {4}(\w+):", m.group(1), re.MULTILINE))

    zh, en = keys("zh"), keys("en")
    assert zh == en, f"STR key mismatch — zh-only: {sorted(zh - en)}, en-only: {sorted(en - zh)}"
