/**
 * Unit tests for the extracted pure functions in harbor-utils.js.
 *
 * Runs with plain Node.js (no test framework) so it works in CI without
 * installing any npm dependencies.  Exit code 0 = all pass, 1 = failure.
 */

const assert = require("assert");
const path = require("path");
const {
  escHTML,
  escAttr,
  needsAttention,
  diffLineClass,
  t,
  cssId,
  STR,
} = require(path.join(__dirname, "..", "..", "src", "harbor", "static", "harbor-utils.js"));

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  ✓ ${name}`);
  } catch (err) {
    failed++;
    console.error(`  ✗ ${name}`);
    console.error(`    ${err.message}`);
  }
}

// ============================================================
// escHTML
// ============================================================
console.log("\nescHTML");

test("escapes ampersand", () => {
  assert.strictEqual(escHTML("a & b"), "a &amp; b");
});

test("escapes angle brackets", () => {
  assert.strictEqual(escHTML("<div>"), "&lt;div&gt;");
});

test("escapes double quotes", () => {
  assert.strictEqual(escHTML('say "hi"'), "say &quot;hi&quot;");
});

test("escapes single quotes", () => {
  assert.strictEqual(escHTML("it's"), "it&#39;s");
});

test("handles empty string", () => {
  assert.strictEqual(escHTML(""), "");
});

test("handles plain text with no special chars", () => {
  assert.strictEqual(escHTML("hello world"), "hello world");
});

test("escapes all special chars together", () => {
  const result = escHTML(`<a href="#" onclick='alert("&")'>`);
  assert.strictEqual(
    result,
    "&lt;a href=&quot;#&quot; onclick=&#39;alert(&quot;&amp;&quot;)&#39;&gt;",
  );
});

// ============================================================
// escAttr
// ============================================================
console.log("\nescAttr");

test("escapes ampersand in attribute", () => {
  assert.strictEqual(escAttr("a&b"), "a&amp;b");
});

test("escapes double quote in attribute", () => {
  assert.strictEqual(escAttr('say "hi"'), "say &quot;hi&quot;");
});

test("does not escape angle brackets (attribute context)", () => {
  // angle brackets inside an attribute value are safe; we only need & and "
  assert.strictEqual(escAttr("<div>"), "<div>");
});

// ============================================================
// needsAttention
// ============================================================
console.log("\nneedsAttention");

const cleanMainRepo = {
  name: "test",
  path: "/tmp/test",
  branch: "main",
  dirty: false,
  detached: false,
  is_main: true,
  ahead: 0,
  behind: 0,
};

test("clean repo on main needs no attention", () => {
  assert.strictEqual(needsAttention(cleanMainRepo), false);
});

test("dirty repo needs attention", () => {
  assert.strictEqual(needsAttention({ ...cleanMainRepo, dirty: true }), true);
});

test("detached repo needs attention", () => {
  assert.strictEqual(
    needsAttention({ ...cleanMainRepo, detached: true, is_main: false }),
    true,
  );
});

test("non-main branch needs attention", () => {
  assert.strictEqual(
    needsAttention({ ...cleanMainRepo, is_main: false, branch: "feature" }),
    true,
  );
});

test("behind upstream needs attention", () => {
  assert.strictEqual(needsAttention({ ...cleanMainRepo, behind: 3 }), true);
});

test("ahead of upstream needs attention", () => {
  assert.strictEqual(needsAttention({ ...cleanMainRepo, ahead: 1 }), true);
});

test("null/undefined behind/ahead defaults to 0 (no attention)", () => {
  // Repos with no upstream have ahead/behind = null
  assert.strictEqual(
    needsAttention({ ...cleanMainRepo, ahead: null, behind: null }),
    false,
  );
  assert.strictEqual(
    needsAttention({ ...cleanMainRepo, ahead: undefined, behind: undefined }),
    false,
  );
});

// ============================================================
// diffLineClass
// ============================================================
console.log("\ndiffLineClass");

test("diff --git line is meta", () => {
  diffLineClass._inHunk = false;
  assert.strictEqual(diffLineClass("diff --git a/foo b/foo"), "d-meta");
});

test("index line is meta", () => {
  diffLineClass._inHunk = false;
  assert.strictEqual(diffLineClass("index abc..def 100644"), "d-meta");
});

test("+++ and --- lines are meta", () => {
  diffLineClass._inHunk = false;
  assert.strictEqual(diffLineClass("+++ a/foo"), "d-meta");
  assert.strictEqual(diffLineClass("--- b/foo"), "d-meta");
});

test("@@ hunk header sets _inHunk and returns d-hunk", () => {
  diffLineClass._inHunk = false;
  assert.strictEqual(diffLineClass("@@ -1,3 +1,4 @@"), "d-hunk");
  assert.strictEqual(diffLineClass._inHunk, true);
});

test("addition inside hunk is d-add", () => {
  diffLineClass._inHunk = true;
  assert.strictEqual(diffLineClass("+new line"), "d-add");
});

test("deletion inside hunk is d-del", () => {
  diffLineClass._inHunk = true;
  assert.strictEqual(diffLineClass("-old line"), "d-del");
});

test("context line inside hunk returns empty string", () => {
  diffLineClass._inHunk = true;
  assert.strictEqual(diffLineClass(" context line"), "");
});

test("addition before hunk header returns empty string", () => {
  diffLineClass._inHunk = false;
  // Not in a hunk yet — this could be a "Binary files differ" line etc.
  assert.strictEqual(diffLineClass("+not in hunk"), "");
});

// ============================================================
// t (i18n)
// ============================================================
console.log("\nt (i18n)");

test("returns Chinese string for zh lang", () => {
  assert.strictEqual(t(STR, "zh", "searchPh"), "过滤 repo…");
});

test("returns English string for en lang", () => {
  assert.strictEqual(t(STR, "en", "searchPh"), "Filter repos…");
});

test("calls function-valued strings with args", () => {
  assert.strictEqual(t(STR, "en", "tagBehind", 5), "5 behind");
  assert.strictEqual(t(STR, "zh", "tagAhead", 3), "领先 3");
});

test("returns undefined for missing key", () => {
  assert.strictEqual(t(STR, "en", "nonexistentKey"), undefined);
});

test("returns undefined for missing lang", () => {
  assert.strictEqual(t(STR, "fr", "searchPh"), undefined);
});

// ============================================================
// i18n key consistency
// ============================================================
console.log("\ni18n key consistency");

test("zh and en have the same set of keys", () => {
  const zhKeys = Object.keys(STR.zh).sort();
  const enKeys = Object.keys(STR.en).sort();
  assert.deepStrictEqual(zhKeys, enKeys, "zh and en STR keys must match");
});

// ============================================================
// cssId
// ============================================================
console.log("\ncssId");

test("prefixes with r-", () => {
  assert.strictEqual(cssId("my-repo"), "r-my-repo");
});

test("encodes special characters", () => {
  // Slashes, spaces, etc. get percent-encoded for safe use as CSS id
  assert.ok(cssId("a/b").includes("%2F"));
});

test("handles empty string", () => {
  assert.strictEqual(cssId(""), "r-");
});

// ============================================================
// Summary
// ============================================================
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
