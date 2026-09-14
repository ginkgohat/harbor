# Harbor 项目评审 —— 可提升项清单

> 分析范围：`src/harbor/`（Python 后端 + 单页前端）、`tests/`、`Makefile`、`pyproject.toml`、`.github/workflows/`。
> 本清单仅做评估，不含代码修改。

---

## 一、架构（Architecture）

**1. 全局可变状态（模块级单例）耦合过强 — `server.py`**
- `app_state`、`AUTH_TOKEN`、`JOBS`/`JOBS_LOCK` 都是模块级全局变量（`server.py:26-42`），`__main__.py:297,303` 通过 `server_mod.app_state = state`、`server_mod.AUTH_TOKEN = token` 注入。
- 影响：隐含依赖、并发/多实例共享状态、测试必须手动 reset。虽然注释承认是有意为之，但本质上仍是 service-locator 味道。建议改为 handler 工厂/闭包注入，状态所有权收进一个 `HarborApp` 对象。

**2. 前端 i18n 字符串表和工具函数双重维护 — 已出现漂移**
- `STR` 表、`escHTML`/`escAttr`/`needsAttention`/`diffLineClass`/`t`/`cssId` 在 `index.html:827-938` 和 `harbor-utils.js:108-237` **各写了一份**，注释明说"kept in sync manually"。
- 证据：`harbor-utils.js` 的 `STR` 缺了 `badToken`/`authSub`/`authTokenPh`/`authSignIn`/`authLost`/`authHelp1`/`authHelp2` 这组键（`index.html` 里有）——两份已经不一致。建议让 `index.html` 引用 `harbor-utils.js`（单源），或生成校验测试。

**3. `do_action` 职责混杂**
- `git.py:334-345` 的 `open-vscode` 用 `subprocess.Popen` + `shutil.which`，与 AGENT.md"git 操作只走 git.py 包装"的约定精神一致，但它同时是唯一 spawn 脱离进程/非 git 命令的地方。建议把它拆到独立模块，让 git.py 只做 git。

**4. `_read_json_body` 无请求体大小上限**
- `server.py:507-513` 直接按 `Content-Length` 读满到内存，无上限。本地工具风险低，但一个失控客户端可令进程 OOM。加一个上限（如 1MB）便宜且稳。

**5. `load_config` 只捕获 `FileNotFoundError`/`TOMLDecodeError`**
- `config.py:45-58` 未捕获 `OSError`（如权限不足），会直接崩溃而非降级为"无配置"。建议 `except OSError: return None`。

**6. 全局 `ThreadPoolExecutor` 每次请求重建**
- `git.py:200-209` 的 `get_repos_status` 每请求 `with ThreadPoolExecutor(8)`，即每次刷新新建 8 个线程。30 秒轮询会频繁建/拆线程池；可考虑共享常驻 executor。

---

## 二、性能（Performance）

**1. 每次轮询对所有 repo 全量跑 `git status`**
- `/api/repos`（`server.py:113-115`）→ `get_repos_status`（`git.py:200`）每 30 秒对每个 repo spawn 一个 `git status --porcelain=v2 --branch` 进程。100+ repo 时每 30 秒 ~100 个 subprocess。无缓存/增量更新（`_default_branch` 已缓存是好的，但 status 本身每次都重算）。
- 建议：文件系统 mtime 脏检测、或仅在 pull/操作后局部失效、或后台定时扫描 + 前端拉取快照。

**2. `_rescan` 同步执行完整扫描 + 逐个探测默认分支**
- `server.py:427-445` 在 HTTP 请求线程里同步 `scan_roots`（`os.walk` 全树 + 每个新 repo 探测 default branch），大目录下可达数秒，阻塞响应。增删 root 也会触发。建议后台线程 + 完成后通知。

**3. `ThreadingHTTPServer` 线程无上限**
- `__main__.py:42` 一连接一线程；SSE `_stream`（`server.py:390-421`）长时间持有线程。本地单用户风险小，但一个卡住/恶意的 keep-alive 连接会占住线程。

**4. SSE 队列无界且客户端断开后不取消**
- `start_pull_all_job`（`server.py:272-299`）的 worker 不感知消费者断开，`queue.Queue` 无界，事件要等到 1 小时 TTL 才被 sweep（`server.py:43,256`）。断开后 worker 仍继续 pull 并堆积事件。建议断开即取消该 job、队列设上限。

**5. `render()` 每次按键全量重渲染 + 多次全表 filter**
- `index.html:1248-1267` 每次搜索输入（`index.html:1388`，无 debounce）做多次 `repos.filter(...)` 并 `innerHTML` 重建整个 grid。数百 repo + 连续输入会明显卡顿。建议：搜索输入 debounce、缓存 filter 结果、按 name 预建索引。

**6. Diff 渲染 5000 个 DOM 节点**
- `index.html:1563,1611-1612` 上限 5000 行，每条一行 `<div>`，仍可能造成一次性长任务卡顿。可考虑 `<pre>` + `textContent` 一次性写入、或虚拟滚动。服务端 512KB 截断（`git.py:275-299`）是已有缓解。

---

## 三、用户体验（UX）

**1. 默认语言写死为中文**
- `<html lang="zh">`（`index.html:2`）且 `localStorage.getItem("harborLang") || "zh"`（`index.html:924`）。对英文用户首次打开是中文，且不代表系统/浏览器语言。建议默认跟随 `navigator.language`。

**2. 主题切换按钮 title 未本地化**
- `index.html:1076-1078` 写死 `"Theme: system"/"light"/"dark"`，与 i18n 约定冲突。

**3. 批量 stash/discard/checkout 与 pull-all 不对称**
- pull-all 走服务端 SSE + 进度条 + 终端的实时流（`index.html:1442-1500`），而 `bulkRun`（`index.html:1631-1636`）用 `Promise.all` 对每个 repo 逐个发 HTTP POST，无 concurrency 上限、无整体进度、完成顺序乱序。语义上"批量"体验割裂。建议统一为服务端 batch job（复用 pull-all 的 SSE 机制）。

**4. `discard` 是不可逆且无安全网的高危操作**
- `git.py:323-326` 执行 `git checkout -- .` + `git clean -fd`，未跟踪文件被**永久删除**，虽有确认弹窗但无"先 stash 后丢弃"的回收路径。建议提供 `git stash -u` 版安全丢弃（可恢复）或至少默认 stash。

**5. `checkout-main` 标签误导**
- 按钮/文案写 "main"（`btnMain`），但 `_default_branch`（`git.py:354-368`）实际可能切到 `master`/`trunk`/`develop`。文案应按真实分支名显示或改叫"默认分支"。

**6. 搜索只匹配 repo 名**
- `index.html:1250` 仅 `r.name` 匹配，无法按 root 标签、路径、分支名搜索。

**7. 部分模态框缺少无障碍焦点陷阱/语义**
- 只有确认弹窗做了 focus trap + focus restore（`index.html:1091-1133`）；diff 弹窗（`#diffOverlay`）和设置弹窗（`#settingsOverlay`）**没有** focus trap/restore，设置弹窗也无 `role="dialog"`/`aria-modal`。`synced-strip` 的 `role="button"` 既在 HTML 内联又在 JS `_initSyncedStripA11y`（`index.html:1137-1158`）重复设置。

**8. 无浏览器级"有待处理"提示**
- 页面 title/图标无变化，切走标签页后无感知。可用 favicon 角标 + 标题数字提示。

**9. 错误降级态误引导**
- `repo_status` 失败时（`git.py:171-178`）返回 `dirty=True, detached=True`，导致 git 不可用/path 失效时所有 repo 显示"待处理"，且 `Discard`/`checkout` 按钮被启用（点击实为无害失败），UI 误导用户。建议区分"错误"与"脏"。

---

## 四、安全（Security）

**1. 认证 token 放在 URL query 上 — 最高优先级**
- token 走 `?token=`（`__main__.py:302,340`）并写入浏览器历史、`window.location`；README 已承认此隐患。`harbor status` 还会明文打印（`daemon.py:224-226`）。建议改用 HttpOnly Cookie 或首屏后转 `Authorization: Bearer` 头，token 不进历史。

**2. Origin/Referer 检查可被 DNS rebinding 绕过**
- `_check_origin`（`server.py:323-342`）仅比较 `parsed.netloc == host`，不看 scheme/回环解析。DNS rebinding 下 `Host` 与 `Origin` 同为攻击者域名即可通过——真正防线其实是 token。这反过来放大了"token 在 URL 可泄露"的第 1 条。

**3. CSP 依赖 `'unsafe-inline'`**
- `server.py:51-56` 的 `script-src 'unsafe-inline'` 意味着任何 HTML 注入都会升级成任意 JS 执行。目前 `escHTML`/`escAttr` 使用较一致（尤其 `termLine` 调用点都包了 `escHTML`），但 `termLine`（`index.html:1432-1439`）本身用 `innerHTML`，未来若有人漏掉转义即 XSS。建议用 nonce/hash 化 CSP，或引入统一 sanitize helper。

**4. `/login` 无速率限制**
- `server.py:86-99` 无尝试锁定/退避。token 熵（`secrets.token_urlsafe(16)` ≈ 132bit）本身足够强，但加个简单限流成本极低。

**5. `get_diff` 未跟踪文件列表存在解析瑕疵**
- `git.py:290-291` 用 `status --porcelain`（v1）并 `line[3:]` 取文件名，未处理 core.quotePath 的 C 风格引用/转义——含空格或特殊字符的文件名会显示原始转义形式。小正确性问题。

**6. `resolve_setting` env 解析无校验、无友好报错**
- `config.py:148-156` 用 `type(default)(env)` 解析，`HARBOR_PORT=abc` 会抛裸 `ValueError` 堆栈；端口范围（1–65535）、depth 非负也未校验，`_create_server` 只会报一个笼统 OSError（`__main__.py:41-55`）。

---

## 五、可维护性 / 工程质量

**1. `_parse_args` 手工扫描 `sys.argv` 脆弱**
- `__main__.py:197-217`：当某个目录/root 恰好叫 `status`/`update`/`start` 等子命令名时会被误判；以 `-` 开头的目录名无法作为 root。可用 argparse 的嵌套处理替代手写启发式。

**2. 类型检查形同虚设**
- `pyproject.toml:102-111` mypy `strict=false`、关闭 `disallow_untyped_defs`、`check_untyped_defs`；`Makefile:97` 甚至 `2>/dev/null || echo 'informational only'`。mypy 目前是"装饰性"门禁，无法真正抓住类型回归。

**3. `requires-python = ">=3.10,<3.15"` 上界需每次发版手动抬**
- `pyproject.toml:7` 与 classifier 列到 3.14。Python 3.15 发布后不抬上界则无法安装，且不抬的原因不明确（无已知不兼容），建议评估是否去掉上界或加注释。

**4. 测试文件单体过大**
- `test_harbor.py`（848 行）、`test_server.py`（970 行）按主题拆分可读性更好。`dist/`、`src/harbor.egg-info/`、`__pycache__/` 等构建产物散落在工作区（虽大概率被 gitignore，但应确保 `make clean` 能清干净）。

**5. `server.py` 有一处重复的 `# Helpers` 注释块（`server.py:226-233`）** —— 纯清洁度问题。

---

## 六、测试与 CI

**1. e2e 测试不上 CI — 主要风险**
- `Makefile:70-71` 的 `test-e2e` 在 `ci.yml` 中**没有对应 job**；AGENT.md 也承认"nothing else will catch a break"当改静态资源/路由时。前端 UI 回归无法被 CI 拦截，只有 JS utils 在跑（`ci.yml:45-51`）。建议加一个 Playwright job（哪怕只跑关键冒烟）。

**2. 缺少校验"两份 STR 表同步"的测试**
- `test_i18n.py`（64 行）规模很小，且既然已发生键漂移，说明没有强制一致性测试。建议让前端逻辑单源化后写 parity 测试。

**3. 覆盖率 70% 可上调 + 后端边界条件补充**
- 阈值（`pyproject.toml:129`）是合理起点；关键路径（config env 解析、daemon fork、SSE 断连、超大 diff 截断边界）值得更多回归测试。

---

## 七、文档

**1. README 的"Zero dependencies / 单 HTML 文件"与事实不符**
- `README.md:9,25` 写"Python standard library + a single HTML file"，但 `pyproject.toml:20-24` 有 `tomli`/`tomli-w`/`platformdirs` 三个运行时依赖，且现在是"HTML + harbor-utils.js"。AGENT.md 已改为"near-zero"，README 需同步。

**2. 安全模型文档已较完整**，但 token-in-URL 的权衡、DNS rebinding 边界建议在 SECURITY.md/README 中更明确地写清。

---

## 优先级建议（速览）

| 优先级 | 项目 |
|---|---|
| 🔴 高 | token 进 URL（安全）、两份 i18n/工具函数漂移（可维护）、e2e 不上 CI（质量）、批量操作体验割裂 + discard 无安全网（UX/数据安全） |
| 🟡 中 | 全量 `git status` 轮询 + 同步 rescan（性能）、`bulkRun` 无并发上限、模块级全局状态、diff 大渲染、Origin 检查可被 DNS rebinding 绕过 |
| 🟢 低 | `_parse_args` 启发式、mypy 装饰性、`<3.15` 上界、diff 未跟踪文件转义、主题 title 未 i18n、默认语言 zh |