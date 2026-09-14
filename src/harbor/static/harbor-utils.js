/**
 * Pure utility functions extracted from the Harbor frontend.
 *
 * This module contains only side-effect-free functions that can be tested
 * in Node.js without a browser.  The inline <script> in index.html uses
 * the same logic (kept in sync manually); this module exists so we can
 * write unit tests and run `node --check` in CI.
 */

// Export everything as a module so Node's test runner and the browser
// can both consume it.
// In the browser, these are attached to `window` by the inline script.

/**
 * Escape a string for safe insertion into HTML text content.
 * @param {string} s
 * @returns {string}
 */
function escHTML(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

/**
 * Escape a string for safe insertion into an HTML attribute value.
 * @param {string} s
 * @returns {string}
 */
function escAttr(s) {
  return s.replace(/[&"]/g, (c) => (c === "&" ? "&amp;" : "&quot;"));
}

/**
 * Determine whether a repo needs attention (shown in the "Needs attention"
 * segment).  A repo needs attention if it has any of:
 *   - uncommitted changes (dirty)
 *   - detached HEAD
 *   - not on the default branch
 *   - behind upstream
 *   - ahead of upstream (forgot to push)
 *
 * @param {object} r - repo status object
 * @returns {boolean}
 */
function needsAttention(r) {
  // ahead > 0 means "forgot to push" — as much a pain point as behind.
  return (
    r.dirty ||
    r.detached ||
    !r.is_main ||
    (r.behind ?? 0) > 0 ||
    (r.ahead ?? 0) > 0
  );
}

/**
 * Return the CSS class for a line of unified diff output.
 *
 * Stateful: uses `diffLineClass._inHunk` to track whether we're inside
 * a hunk body (lines after the @@ hunk header).
 *
 * @param {string} line
 * @returns {string}
 */
function diffLineClass(line) {
  if (line.startsWith("diff --git") || line.startsWith("index ")) return "d-meta";
  if (line.startsWith("+++") || line.startsWith("---")) return "d-meta";
  if (line.startsWith("@@")) {
    diffLineClass._inHunk = true;
    return "d-hunk";
  }
  if (diffLineClass._inHunk) {
    if (line.startsWith("+")) return "d-add";
    if (line.startsWith("-")) return "d-del";
  }
  return "";
}

/**
 * Translate a string key for the current language.
 * @param {object} STR - i18n string table
 * @param {string} lang - language code (e.g. "zh", "en")
 * @param {string} key - string key
 * @param  {...any} args - arguments passed if the value is a function
 * @returns {string|undefined}
 */
function t(STR, lang, key, ...args) {
  const v = STR[lang]?.[key];
  return typeof v === "function" ? v(...args) : v;
}

/**
 * CSS-safe id for a repo (uses encodeURIComponent to escape special chars).
 * @param {string} name
 * @returns {string}
 */
function cssId(name) {
  return "r-" + encodeURIComponent(name);
}

// -- i18n string table (mirrors the one in index.html) -----------------------

const STR = {
  zh: {
    searchPh: "过滤 repo…",
    segAttention: "待处理",
    segAll: "全部",
    pullAll: "Pull all",
    sectionAttention: "待处理",
    sectionAll: "全部 repo",
    sectionSynced: "已同步",
    syncedSuffix: "个 repo 已同步到 main / master，无需处理",
    emptyOk: "✓ 一切正常，没有需要处理的 repo",
    tagDirty: "改动未提交",
    tagDetached: "detached",
    tagBehind: (n) => `落后 ${n}`,
    tagAhead: (n) => `领先 ${n}`,
    btnPull: "Pull",
    btnStash: "Stash",
    btnDiscard: "丢弃",
    btnMain: "默认分支",
    btnPreview: "预览",
    btnVscode: "VSCode",
    refreshTip: "刷新配置",
    refreshDone: (n) => `已刷新 · ${n} 个 repo`,
    batchStash: (n) => `Stash 全部 (${n})`,
    batchDiscard: (n) => `丢弃全部 (${n})`,
    batchMain: (n) => `切换全部到默认分支 (${n})`,
    statLine: (total, dirty, nonMain) =>
      `<b>${total}</b> repos · <b>${dirty}</b> 未提交 · <b>${nonMain}</b> 非 main`,
    confirmDiscardTitle: "丢弃改动",
    confirmDiscardBody: (name) =>
      `将在 "${name}" 把所有未提交改动和未追踪文件放入 git stash（可恢复），可随时用 "git stash apply" 找回。`,
    confirmMainTitle: "切换到默认分支",
    confirmMainBody: (name) => `将把 "${name}" 切换到其默认分支。`,
    confirmBatchDiscardTitle: "丢弃全部改动",
    confirmBatchDiscardBody: (n, names) =>
      `将在 ${n} 个 repo（${names}）把所有未提交改动放入 git stash（可恢复）。`,
    confirmBtn: "确认执行",
    cancelBtn: "取消",
    termTitle: "终端日志",
    termClear: "清空",
    diffLoading: "加载中…",
    diffLoadingBody: "加载改动中…",
    diffLoadFail: "无法加载改动",
    diffChanged: (n) => `${n} 行改动`,
    diffUntracked: (n, files) => `+ ${n} 个新文件（未跟踪）：${files}`,
    diffEmpty: "没有可显示的改动",
    diffTruncated: "⚠️ diff 过大，服务端已截断（仅前 512KB）",
    diffTooManyLines: (n) => `⚠️ 行数过多，仅渲染前 ${n} 行`,
    connError: "⚠️ 无法连接后端服务 —— 请确认 harbor 正在运行",
    connTimeout: "⚠️ 请求超时 —— 某个仓库的 git 命令可能卡住了",
    retry: "重试",
    connFailed: "连接失败",
    badToken: "Token 无效，请重新输入",
    authSub: "登录以继续",
    authTokenPh: "访问令牌",
    authSignIn: "登录",
    authLost: "忘记 token？",
    authHelp1: "在终端运行 <code>harbor status</code>",
    authHelp2: "或查看运行 Harbor 的终端里的启动输出",
    settingsTitle: "仓库",
    addDirectory: "添加目录",
    noRoots: "还没有配置目录 —— 在下方添加",
    noSubdirs: "没有子目录",
    addBtn: "添加",
    removeTip: "移除",
    themeSystem: "主题：跟随系统",
    themeLight: "主题：浅色",
    themeDark: "主题：深色",
  },
  en: {
    searchPh: "Filter repos…",
    segAttention: "Needs attention",
    segAll: "All",
    pullAll: "Pull all",
    sectionAttention: "Needs attention",
    sectionAll: "All repos",
    sectionSynced: "Synced",
    syncedSuffix: "repos already synced to main / master",
    emptyOk: "✓ All clear — nothing needs attention",
    tagDirty: "uncommitted",
    tagDetached: "detached",
    tagBehind: (n) => `${n} behind`,
    tagAhead: (n) => `${n} ahead`,
    btnPull: "Pull",
    btnStash: "Stash",
    btnDiscard: "Discard",
    btnMain: "default",
    btnPreview: "Preview",
    btnVscode: "VSCode",
    refreshTip: "Refresh config",
    refreshDone: (n) => `Refreshed · ${n} repos`,
    batchStash: (n) => `Stash all (${n})`,
    batchDiscard: (n) => `Discard all (${n})`,
    batchMain: (n) => `Switch all to default branch (${n})`,
    statLine: (total, dirty, nonMain) =>
      `<b>${total}</b> repos · <b>${dirty}</b> uncommitted · <b>${nonMain}</b> non-main`,
    confirmDiscardTitle: "Discard changes",
    confirmDiscardBody: (name) =>
      `This will stash all uncommitted changes and untracked files in "${name}" (recoverable). You can restore them anytime with "git stash apply".`,
    confirmMainTitle: "Switch to default branch",
    confirmMainBody: (name) => `This will check out the default branch in "${name}".`,
    confirmBatchDiscardTitle: "Discard all changes",
    confirmBatchDiscardBody: (n, names) =>
      `This will stash uncommitted changes in ${n} repos (${names}) (recoverable).`,
    confirmBtn: "Confirm",
    cancelBtn: "Cancel",
    termTitle: "Terminal log",
    termClear: "Clear",
    diffLoading: "Loading…",
    diffLoadingBody: "Loading changes…",
    diffLoadFail: "Failed to load changes",
    diffChanged: (n) => `${n} lines changed`,
    diffUntracked: (n, files) => `+ ${n} untracked file(s): ${files}`,
    diffEmpty: "No changes to show",
    diffTruncated: "⚠️ Diff too large — truncated server-side to the first 512KB",
    diffTooManyLines: (n) => `⚠️ Too many lines — only the first ${n} are rendered`,
    connError: "⚠️ Cannot connect to backend — make sure harbor is running",
    connTimeout: "⚠️ Request timed out — a repo's git command may be stuck",
    retry: "Retry",
    connFailed: "Connection failed",
    badToken: "Invalid token, please try again",
    authSub: "Sign in to continue",
    authTokenPh: "Access token",
    authSignIn: "Sign in",
    authLost: "Lost your token?",
    authHelp1: "Run <code>harbor status</code> in your terminal",
    authHelp2: "Or check the startup output in the terminal where Harbor is running",
    settingsTitle: "Repositories",
    addDirectory: "Add directory",
    noRoots: "No roots configured yet — add one below.",
    noSubdirs: "No subdirectories",
    addBtn: "Add",
    removeTip: "Remove",
    themeSystem: "Theme: system",
    themeLight: "Theme: light",
    themeDark: "Theme: dark",
  },
};

// -- Exports -----------------------------------------------------------------

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    escHTML,
    escAttr,
    needsAttention,
    diffLineClass,
    t,
    cssId,
    STR,
  };
}
