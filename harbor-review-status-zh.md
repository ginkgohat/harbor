# Harbor 评审改进 —— 任务清单与状态

本文件是 `harbor-review-zh.md`（原评审清单）的配套活文档，记录已改进项、剩余任务和关键决策。
每完成一项，在「已完成」表格里追加一行，并从「剩余任务」勾掉对应勾选框。

> 维护：每完成一批改动后更新此文件（数量、验证结果、决策）。条目保持简短，详细讨论见对应工具的调用历史。

---

## 一、快速状态

| | 数量 |
|---|---|
| ✅ 已完成 | 28 |
| 🕐 剩余待办 | 0 组（较高/中/低全部清空；两个可选 follow-up 也已做完） |
| 基线 | 本地：196 测试（170 非 e2e + 22 e2e）全绿；ruff 干净；前端 JS 34 通过；mypy `--strict` 0 错 |
| CI | `lint` / `build` / `test`（5 矩阵）/ `e2e`（官方 Playwright 镜像容器，免 apt 免浏览器下载） |

---

## 二、已完成 ✅

| # | 项 | 说明 | 验证 |
|---|---|---|---|
| 1 | token 进 URL → 签名会话 cookie（安全） | 启动 token 一次性兑换，`/?token=` → 302 → `harbor_session` cookie（HMAC、host:port 绑定、HttpOnly、SameSite=Strict、Max-Age=30d）；`SESSION_SECRET` 0600 持久化于 `$STATE/harbor.credentials`；启动 token 永不被 `/api` 接受。 | 181 测试含 auth 全套 |
| 2 | 两份 i18n/工具函数漂移（可维护） | 新增 STR 表 zh/en 跨文件同步测试 `test_str_tables_in_sync_across_files`。 | test_i18n 通过 |
| 3 | 批量体验割裂 + discard 无安全网（UX） | 批量 stash/discard/checkout 合并到 `bulkRun`；discard 改为可恢复的 `git stash push -u -m "harbor:discard <ts>"`。 | test_do_action_discard 扩展,验证 stash 可找回 |
| 4 | `_parse_args` 启发式加固（可维护） | 抽 `SUBCOMMANDS`/`TOP_LEVEL_FLAGS`/`END_OF_OPTIONS` 常量 + `--` 转义（`harbor -- status`、`harbor -- -my-dir`）。 | tests/test_cli.py,9 用例 |
| 5 | checkout-main 标签误导（UX#5） | 文案 "main" → "default branch / 默认分支"（zh+en STR 表同步）。 | i18n 同步测试 |
| 6 | 搜索范围（UX#6） | 从仅 repo 名 → name/path/root_label/branch 多字段。 | — |
| 7 | bulkRun 并发上限（Perf#3） | 无界 `Promise.all` → 并发 4 的 worker 池 + `x/N done` 进度。 | — |
| 8 | e2e 上 CI（测试） | 独立 job，单 `ubuntu-latest` + Python 3.12；`playwright install --with-deps chromium` + `make test-e2e`。 | ci.yml YAML 校验 |
| 9 | 基础加固（安全） | `MAX_BODY_BYTES` 413；`_is_loopback_host` / `_check_origin` 非 loopback Host 403；配置 `OSError` 降级；`resolve_int_setting` 校验端口/深度。 | 对应单测 |
| 10 | 主题 title i18n / 默认语言 | 默认 `lang` 取 `navigator.language`；theme title 加 i18n STR。 | — |
| 11 | diff 未跟踪文件转义（安全#5）+ 持久 executor（Arch#6） | `get_diff` 用 `--porcelain -z` 取未跟踪路径；模块级 `ThreadPoolExecutor`。 | get_diff/untracked 测试 |
| 12 | 文档 | README "Zero/single-HTML" 措辞 → "Near-zero dependencies"。 | — |
| 13 | SSE 队列上界 | `queue.Queue(maxsize=MAX_SSE_QUEUE_EVENTS=1024)` 加背压上界，避免无消费者时事件无限积压。 | test_pull_all_job_queue_is_bounded |
| 14 | `/login` 失败限流 | 每客户端滚动窗口（60s 内最多 5 次失败）后返回 429；成功登录清空计数。 | test_login_rate_limits / resets |
| 15 | 模态框焦点陷阱 | 既有实现确认到位（Tab 循环 + 返回焦点，T-043，本会话核对）。 | — |
| 16 | 批量操作 pending 指示 | bulkRun 运行期间禁用 `#batchActions` 按钮（可见 pending + 防重复触发）。 | node --check |
| 17 | `SECURITY.md` token↔cookie | Security Model 补：launch token 一次性兑换签名 HttpOnly 会话 cookie，永不进 API、离开地址栏/历史。 | — |
| 18 | `SECURITY.md` discard=stash | Security Model 注明 discard 为可恢复的 `git stash push -u`（`harbor:discard <ts>`），可 `stash apply` 撤回。 | — |
| 19 | Makefile `e2e-setup --with-deps` | 与 CI 行为一致，1 行。 | (`make test-e2e`) |
| 20 | diff 大渲染（Perf#6） | `openDiff` 改用单个 `<pre>` + 一次 `textContent` 写入，替代逐行至多 5000 个 `<div>`（一次性 long-task → 单文本节点）；换取单色，changed 行数保留在副标题。移除内联 `diffLineClass`。 | e2e `test_diff`、node --check |
| 21 | DNS rebinding（安全#2） | 完成项 #9 的 `_is_loopback_host` Host 校验本已阻塞非回环 Host（rebinding 下 Host=攻击者域名 → 403），是核心防线；SECURITY.md 补边界说明 + 既有测试佐证。 | `test_is_loopback_host` / `test_mutating_request_rejects_non_loopback_host` |
| 22 | mypy strict（L1） | 全部 10 个源文件补全注解，`mypy --strict src/harbor` 从 153 错 → 0 错；`pyproject.toml` `strict=true`；Makefile `lint` 去掉 `|| echo`，mypy 成为真实 CI 门禁。顺手修真隐患：`hmac.new` 的 `SESSION_SECRET` None 未排除、`with os.fdopen(...,"wb") as f` 复用变量名。 | `make lint`、mypy --strict |
| 23 | `requires-python` 上界（L2） | `>=3.10,<3.15` 已在位且与分类器一致（本地验证于 3.14.7）；无代码改动，留待 3.15 发布时复查。 | `python3.14` import/运行 |
| 24 | 模块级全局状态解耦（M2/Arch#1） | 6 个模块级单例（app_state、AUTH_TOKEN、SESSION_SECRET、JOBS/JOBS_LOCK、_LOGIN_FAILURES/_LOGIN_LOCK）收敛为单个 `HarborApp`（state.py，由 AppState 更名）；锁按实例独立；`__main__` 不再 `server_mod.AUTH_TOKEN=...` 逐项注入；测试 fixture 改为一次替换全量重置。 | 162+22 全绿 |
| 25 | 后台轮询 + 快照缓存（M1/Perf#1） | `/api/repos` 每 30s 每 repo 一个 `git status` 子进程 → 单条 daemon 刷新线程每周期算一次全量快照（共享 executor），请求端为 O(1) 缓存读；rescan 完成即失效快照、action 后按路径更新快照、快照为空且 repos 非空时按需现场计算兜底。新增 6 测试。 | 168+22 全绿 |
| 26 | rescan 提速（M1/Perf#2） | `scan_roots` 每 repo 1–6 个子进程探测 default branch 是主导成本（60 repo 实测 1.00s）；新增跨扫描 TTL 缓存（300s），热 rescan 0.001s。完整异步 rescan（立即返回+通知）评估后不取，记入决策表。新增 2 测试。 | 170+22 全绿 |
| 27 | 完整异步 rescan（follow-up） | `POST /api/rescan` 立即返回 `{ok,pending}`，扫描在 daemon worker 上跑；worker 换入新 repo 集 + 预计算状态快照 + 递增 `scan_generation`（`/api/repos` 经 `X-Harbor-Scan-Gen` 头上报）；前端 `waitForScan` 轮询到 generation 前进才更新网格（refresh / 增删 root 三处统一）；单飞 + `rescan_requested` 重跑旗标防遗漏。响应契约从同步 `{roots,count,...}` 改为 `{ok,pending}`（已同步更新测试）。 | 170+22 全绿 |
| 28 | CI e2e 瘦身（follow-up） | e2e job 改用官方 `mcr.microsoft.com/playwright/python:v1.62.0-noble` 容器（内置 py3.12 + Chromium + 全部系统库）；pin `playwright==1.62.0` 与镜像版本对齐 → `playwright install chromium` 为 0.2s no-op；job 内联 make 对应三条命令（镜像无 make）免 apt。顺手修 browse e2e 对「空家目录」的环境脆弱断言（容器 root 家目录只有隐藏目录）。容器内实测 22/22 全绿。 | 容器内 22/22、宿主 170+22 |

---

## 三、剩余任务 🕐

### 高优先级

- [x] **H1. e2e 上 CI**
  状态：✅ **已完成**（见二.#8，2025 由本清单采纳）。
  ✅ 后续瘦身已做：e2e job 改用官方 Playwright 镜像容器，免 apt 免浏览器下载（见二.#28）。

### 中优先级

- [x] **M1. 全量 `git status` 轮询 + 同步 rescan（Perf#1/#2）—— ✅ 已完成（见二.#25/#26）**
  - Perf#1：后台常驻刷新线程 + 快照缓存，`/api/repos` 变成 O(1) 缓存读。
  - Perf#2：跨扫描 default_branch 缓存（TTL 300s），实测 60 repo 冷扫描 0.85s → 热 rescan 0.001s。
  - 完整异步 rescan（立即返回 + 通知）✅ 后续已做（见二.#27）：`/api/rescan` 立即返回，扫描在 daemon worker 上跑，前端轮询 generation 更新。

- [x] **M2. 模块级全局状态解耦（Arch#1）—— ✅ 已完成（见二.#24）**
  - 6 个模块级单例（app_state、AUTH_TOKEN、SESSION_SECRET、JOBS/JOBS_LOCK、_LOGIN_*）收敛为单个 `HarborApp` 对象（state.py），锁按实例独立。
  - 测试 fixture 一次替换即全量重置；多实例可在同一进程共存。

- [x] **M3. diff 大渲染虚拟化（Perf#3）—— ✅ 已完成（见二.#20，选轻量方案：`<pre>`+`textContent` 单文本节点，放弃逐行高亮；全局无需完整虚拟滚动）**

- [x] **M4. DNS rebinding 加固（安全）—— ✅ 已完成（见二.#21）**
  - 现状：Origin 检查可被"恶意域名解析到 127.0.0.1"绕过。
  - 结论：已由 `_is_loopback_host` Host 校验（#9 副作用）堵住；SECURITY.md 已补边界，确认既有测试。

### 低优先级

- [x] **L1. mypy strict —— ✅ 已完成（见二.#22）**
  现状：`pyproject.toml` `strict=true`；`make lint` 中 mypy 为真实门禁（无 `|| echo`）。

- [x] **L2. `requires-python` 上界 —— ✅ 已完成（见二.#23）**
  现状：`>=3.10,<3.15` 已在位，与 3.10–3.14 分类器一致；本地 3.14.7 验证；待 3.15 发布复查。

- [x] **L3. 小项集合 —— ✅ 全部完成（见二.#13–19）**

---

## 四、关键决策记录

| 主题 | 决策 | 理由 |
|---|---|---|
| API 认证 | 会话 cookie（不用 Bearer/首 token） | token 进 URL 会泄漏进日志/历史；cookie 更贴近普通浏览器安全模型 |
| discard | `git stash push -u`（可恢复）而非 `checkout -- . && clean -fd` | 提供撤回路径；保留"离开工作区"效果 |
| M2 状态所有权 | 所有可变状态收敛进单个 `HarborApp` 对象，保留唯一注入点 `server.app_state`；不做 handler 构造器/闭包注入 | 评审建议的闭包注入需重写 ~110 行测试 harness，零用户收益；单对象 + 原子重置已消除散落全局与跨测试污染，多实例可在同一进程共存 |
| M1 Perf#1 | 后台常驻刷新线程 + 快照缓存，`/api/repos` 为缓存读；空快照且非空 repos 时按需现场计算兜底 | 消除每轮询每个 repo 一个子进程；跨标签页共享一次刷新；响应形状不变，前端零改动 |
| M1 Perf#2 | 跨扫描 default_branch 缓存（TTL 300s）替代完整异步 rescan | 实测热 rescan 0.001s（原 1.00s）；异步需改 /api/rescan 契约 + 前端轮询 + e2e 时序，为残余单次 os.walk 不值当 |
| bulkRun 并发 | 客户端 worker 池（并发 4）+ 进度 | 低成本解决无上限/乱序；不强上服务端 SSE job 引擎 |
| e2e CI | 独立 job，单 Linux + 单 Python | e2e 测浏览器行为，与版本/OS 无关；避免矩阵重复 |
| M1 异步 rescan（follow-up） | `/api/rescan` 改为立即返回 `{ok,pending}`，扫描放 daemon worker（单飞 + 重跑旗标）；前端轮询 `X-Harbor-Scan-Gen` 头等 generation 前进 | 大目录树不再阻塞请求线程；先前因「残余成本仅单次 os.walk」暂缓，用户选择跟进实现 |
| e2e CI 镜像（follow-up） | e2e job 用官方 Playwright Python 镜像容器 + pin `playwright` 版本对齐（1.62.0）；放弃 `--with-deps` 与浏览器下载 | 镜像内置 py3.12+Chromium+系统库；apt 步骤容器内实测 55s，镜像省掉；pin 使 `playwright install` 为 no-op。代价：playwright 发版需同步 pyproject 与镜像 tag |
| `_parse_args` | 既有“无子命令→serve”启发式 + `--` 转义 | argparse 无法简洁表达“子命令 XOR 默认 serve”；最小改动 |
| `/login` 限流 | 每客户端滚动窗口计数（60s / 5 次）后 429，成功后清零 | 单机本地工具，按客户端维度即可；无需全局/分布式锁 |
| SSE 队列 | 有界 `Queue(maxsize=1024)` 提供背压而非无限积压 | 无消费者时不再无界占用内存；worker 为 daemon 线程，阻塞不影响进程退出 |

---

## 五、验证基线命令

```bash
# 非 e2e
PYTHONPATH=src python -m pytest tests/ -q -m 'not e2e'
# e2e（需 Chrome）
PYTHONPATH=src python -m pytest tests/e2e/ -m e2e --browser chromium
# lint
ruff check src/ tests/
node --check src/harbor/static/harbor-utils.js
node tests/frontend/test-utils.js
```