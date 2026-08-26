# Harbor 改进任务拆解

> 来源：2024-08 项目全面评审（架构 / 代码 / 安全 / 性能 / 前端 / 测试 / 发布）。
> 每个任务是**可独立开发、独立验收**的最小交付单元。
> 可行性验证结论见同目录 `feasibility-validation.md`。

## 状态图例

`☐ 待办` · `🔄 进行中` · `✅ 完成` · `❌ 已否决`

---

## P0 — 确认的 Bug（立即修）

### T-001 修复 scanner 漏检 git worktree / submodule 的 `.git`
- **状态**: ☐ 待办
- **问题**: `scanner.py` 用 `".git" in dirnames` 判断，但 worktree 和 submodule 的 `.git` 是**文件**（内容为 `gitdir: ...`），出现在 `filenames` 中，导致这些仓库被静默漏掉。已实验复现（见 validation §V-01）。
- **改动点**: `src/harbor/scanner.py::find_repos`
  ```python
  if ".git" in dirnames or ".git" in _filenames:
  ```
- **验收标准**:
  - [ ] 新增测试：`git worktree add` 创建的目录能被发现
  - [ ] 新增测试：含 `.git` 文件的伪 repo 目录能被发现
  - [ ] 现有 49 个测试全部通过
- **预估**: 0.5h ｜ **依赖**: 无

### T-002 修复 pull-all 任务队列泄漏（JOBS 永不清理）
- **状态**: ☐ 待办
- **问题**: `server.py` 中 `JOBS.pop()` 仅在 SSE 消费者读到 `done` 时执行。客户端中途断开 → `done` 事件无人消费 → job 条目与 queue 永久驻留，内存泄漏。已实验确认无任何清理机制（validation §V-02）。
- **改动点**: `src/harbor/server.py`
  - job 记录增加 `created = time.monotonic()`
  - `start_pull_all_job()` 入口惰性清扫 TTL > 3600s 的旧 job
  - （可选加固）worker `finally` 中在 put done 后延迟 pop
- **验收标准**:
  - [ ] 集成测试：SSE 客户端提前断开，job 在 TTL 后被清除
  - [ ] 正常完成路径的清理行为不回归
- **预估**: 1h ｜ **依赖**: 无

### T-003 补齐 i18n 硬编码文案
- **状态**: ☐ 待办
- **问题**: 已定位三处硬编码（validation §V-03）：
  - L794 `render()` root 筛选按钮硬编码「全部」
  - L501 静态 HTML 同上
  - Settings 弹窗整块英文（"Repositories"、"Add directory"、"No roots configured yet"、"No subdirectories"）
- **改动点**: `index.html` —— 全部收口进 `STR.zh/en`；静态节点改由 `renderStatic()` 填充
- **验收标准**:
  - [ ] 切换 EN 后 UI 无任何中文字符残留；切换 ZH 后 Settings 弹窗无英文残留
  - [ ] CI 增加 grep 脚本：HTML 内 CJK 字符串必须出现在 STR 表中（防回归）
- **预估**: 1.5h ｜ **依赖**: 无

---

## P1 — 安全加固

### T-010 Origin 校验收口到 dispatch 层
- **状态**: ☐ 待办
- **问题**: `_check_origin()` 目前只挂在 `/api/repo/<path>/action`（grep 确认仅 1 处调用），而 `POST /api/pull-all`、`/api/rescan`、`POST/DELETE /api/roots` 均未校验。
- **改动点**: `server.py::_dispatch` 对所有非 GET 请求统一校验；删除路由内的零散调用。
- **验收标准**:
  - [ ] 参数化测试覆盖全部变更类路由的跨域拒绝
  - [ ] 同源请求不受影响
- **预估**: 1h ｜ **依赖**: 无

### T-011 本地 token 鉴权（防 CSRF / DNS rebinding / 本地恶意进程）
- **状态**: ☐ 待办
- **问题**: discard 是不可逆数据破坏操作，当前任何本地进程或 rebinding 页面都可调用。
- **方案**: 启动生成 `secrets.token_urlsafe(16)`；服务 URL 为 `http://127.0.0.1:<port>/?token=<hex>`；中间件校验 query/header；token 不匹配返回 403。静态资源放行。
- **验收标准**:
  - [ ] 无 token 访问 API 返回 403
  - [ ] 带 token 的完整用户流程（浏览器手工 + e2e）正常
  - [ ] README 更新说明
- **预估**: 3h ｜ **依赖**: 无

### T-012 增加 Content-Security-Policy 响应头
- **状态**: ☐ 待办
- **问题**: 当前未设置 CSP（grep 确认）。内联脚本需要 `'unsafe-inline'`，但仍应阻断外联与外部资源。
- **改动点**: `_serve_html` / `_serve_static_file` 加头：
  ```
  default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'
  ```
- **验收标准**: 页面功能在 CSP 下无 console 报错；curl 验证响应头存在。
- **预估**: 0.5h ｜ **依赖**: 无

### T-013 禁止 git 凭据交互式提示挂起
- **状态**: ☐ 待办
- **改动点**: `git.run_git` 传入 `env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}`
- **验收标准**: 单元测试断言 env 传递；私有 repo 凭据过期时快速失败而非等满 120s timeout。
- **预估**: 0.5h ｜ **依赖**: 无

---

## P1 — 性能优化

### T-020 用 `status --porcelain=v2` 合并 status 子进程（6→1）
- **状态**: ☐ 待办
- **问题**: `repo_status` 每 repo 跑 4–6 个子进程；每次前端点击都全量刷新所有 repo。
- **验证结论**: 单条 `git status --porcelain=v2 --branch` 可同时给出 branch / dirty(含staged) / ahead(`# branch.ab +2 -0`) / behind / detached，已在本机实测（validation §V-04）。
- **改动点**: 重写 `git.repo_status` 为单命令 + 解析器；解析器写成纯函数便于单测。
- **验收标准**:
  - [ ] 解析器对 dirty/staged/detached/ahead/behind/no-upstream 全分支覆盖单测
  - [ ] 与旧实现输出对比测试（同一批 fixture repo 结果一致）
  - [ ] 50 repo 场景 `/api/repos` 耗时对比记录进 PR 描述
- **预估**: 3h ｜ **依赖**: 无

### T-021 缓存默认分支探测结果
- **状态**: ☐ 待办
- **问题**: `_default_branch()` 每次 status 都跑 `symbolic-ref` + 最多 5 次 `rev-parse`。默认分支几乎不变。
- **改动点**: 扫描时计算一次存入 repo 记录；rescan 刷新。
- **验收标准**: 单测覆盖缓存命中/失效；`is_main` 行为不回归。
- **预估**: 1.5h ｜ **依赖**: T-020（避免冲突改同一函数）

### T-022 前端增量刷新（消灭全量 reload）
- **状态**: ☐ 待办
- **改动点**: 单个 action 响应体带回该 repo 最新 status，前端只更新对应卡片数据；批量操作仅在 done 时做一次全量刷新。
- **验收标准**: 点击 Pull 后网络面板只有 1 个 action 请求（不再触发 `/api/repos`）；卡片数据正确更新。
- **预估**: 3h ｜ **依赖**: T-020

---

## P1 — 架构重构

### T-030 AppState 替代 Handler 类属性全局状态
- **状态**: ☐ 待办
- **问题**: `repos/min_depth/max_depth/cli_*` 全挂类属性，测试被迫写快照 fixture 防串扰。
- **改动点**: 引入 `AppState` dataclass；`__main__` 构造注入 handler；删除 `_restore_handler_state` fixture。
- **验收标准**: 测试直接构造 AppState 实例；fixture 删除后测试套件稳定。
- **预估**: 4h ｜ **依赖**: 无（建议在 T-020 之后做以减少 rebase 冲突）

### T-031 git 层去 HTTP 化 + Repo dataclass + safe_pull 去重
- **状态**: ☐ 待办
- **改动点**:
  - `do_action` 返回领域结果 `ActionOutcome(ok, output)`，HTTP 映射移入 server
  - `Repo` dataclass 替代裸 dict（`asdict()` 序列化给前端）
  - 抽取 `safe_pull(path)` 合并 `pull_one` 与 `do_action("pull")` 的重复跳过逻辑
- **验收标准**: `harbor.git` 模块 import 不含 http 概念；重复逻辑消除且行为不变。
- **预估**: 4h ｜ **依赖**: T-030

### T-032 Ruff 规则加码 + 渐进式 mypy
- **状态**: ☐ 待办
- **改动点**: ruff select 追加 `UP, SIM, RUF, C4` 并修复告警；mypy 先只查 `src/harbor`、宽松配置起步；加 `.pre-commit-config.yaml`（ruff + ruff-format + 基础 hooks）。
- **验收标准**: `make lint` 含新规则全绿；pre-commit 本地可用；CI 加 pre-commit job（可选）。
- **预估**: 2h ｜ **依赖**: 建议 T-031 之后（减少返工）

---

## P1 — 前端 / UI

### T-040 显示 ahead 徽章（↑n）
- **状态**: ☐ 待办
- **问题**: 后端已有 `ahead` 字段，前端 0 处引用（grep 确认）；「忘了 push」恰是核心痛点。
- **验收标准**: ahead>0 的 repo 卡片显示 ↑n；i18n 两语言齐备。
- **预估**: 0.5h ｜ **依赖**: 无

### T-041 Diff 大小截断保护
- **状态**: ☐ 待办
- **问题**: `git diff HEAD` 输出整体进内存并逐行建 DOM，lockfile 级 diff 会冻结页面。
- **改动点**: 服务端按 512KB 截断并带 `truncated: true` 标记；前端超 ~5000 行只渲染前 N 行 + 截断提示。
- **验收标准**: 构造超大 diff 的测试 repo，预览不卡顿且有明确截断提示。
- **预估**: 2h ｜ **依赖**: 无

### T-042 卡片显示最近提交信息
- **状态**: ☐ 待办
- **改动点**: `repo_status` 增加 `git log -1 --format=%cs %s` 字段（随 T-020 一并合并进单次调用链）；卡片展示相对时间 + message。
- **验收标准**: 有 commit / 空 repo（ unborn branch）均正常显示。
- **预估**: 2h ｜ **依赖**: T-020

### T-043 可访问性补齐（modal focus trap / aria / 键盘）
- **状态**: ☐ 待办
- **改动点**: dialog role + aria-modal + 焦点圈闭 + 关闭归还焦点；图标按钮 aria-label；synced-strip 可聚焦可回车。
- **验收标准**: 纯键盘可完成「打开设置→添加目录→关闭」全流程。
- **预估**: 3h ｜ **依赖**: 无

### T-044 暗色模式三态切换
- **状态**: ☐ 待办
- **改动点**: `data-theme` 属性 + localStorage 持久化 + 工具栏切换按钮（系统/亮/暗），复用现有 CSS 变量结构。
- **预估**: 1.5h ｜ **依赖**: 无

### T-045 前端逻辑抽测 + JS 语法 CI 校验
- **状态**: ☐ 待办
- **改动点**: 将纯函数（`needsAttention`/`diffLineClass`/`escHTML`/STR 完整性）抽到可被 Node 直接执行的模块；CI 加 `node --check`；STR 双语言 key 集合一致性断言（配合 T-003）。
- **验收标准**: `make test-js` 进 Makefile 与 CI；至少 10 个前端单测。
- **预估**: 3h ｜ **依赖**: 无

---

## P1 — 测试建设

### T-050 SSE 流集成测试（覆盖泄漏修复）
- **状态**: ☐ 待办
- **改动点**: 线程起真实 ThreadingHTTPServer + urllib 读流；用例：正常 done、客户端提前断开（联动 T-002）、并发双 job。
- **验收标准**: 三条路径均有断言；总耗时 < 5s。
- **预估**: 3h ｜ **依赖**: T-002

### T-051 scanner 属性测试（hypothesis）
- **状态**: ☐ 待办
- **改动点**: dev extra 加 hypothesis；不变式：结果 path 必在扫描根下、path 唯一、worktree 不漏（联动 T-001）。
- **预估**: 2h ｜ **依赖**: T-001

### T-052 Playwright e2e 冒烟（独立 e2e extra）
- **状态**: ☐ 待办
- **改动点**: `e2e` extra 装 playwright；起 server → 断言卡片渲染 / 确认弹窗 / 语言切换；CI 单独 job 仅在 main 分支跑。
- **预估**: 4h ｜ **依赖**: T-011（token 流程确定后）

### T-053 `make demo` 一键演示环境
- **状态**: ☐ 待办
- **改动点**: 脚本创建 dirty/behind/clean/detached 四种状态的假 repo 并起 harbor。
- **预估**: 1h ｜ **依赖**: 无

---

## P2 — 功能路线图

### T-060 Fetch All（优先级最高的新功能）
- **状态**: ☐ 待办
- **说明**: 并发 `git fetch --prune`，只更新 ahead/behind 不动工作区；完全复用现有 job/SSE 机制；工具栏新增按钮。
- **预估**: 4h ｜ **依赖**: T-020（status 数据新鲜度）

### T-061 Repo 详情视图
- **状态**: ☐ 待办
- **说明**: 点击卡片展开最近提交列表 / 本地分支 / stash 列表 / remote 信息；后端新增 `/api/repo/<path>/detail`。
- **预估**: 8h ｜ **依赖**: T-031

### T-062 忽略规则配置
- **状态**: ☐ 待办
- **说明**: config 支持 `[[ignore]]` glob；scanner 过滤；Settings 弹窗管理。
- **预估**: 4h ｜ **依赖**: T-031

### T-063 Open-in 终端 / Finder / Explorer
- **状态**: ☐ 待办
- **说明**: 可配置 open-with 命令模板，替代硬编码 vscode。
- **预估**: 3h ｜ **依赖**: 无

### T-064 显式批量多选模式
- **状态**: ☐ 待办
- **说明**: 卡片 checkbox + 底部浮动操作栏，替代启发式批量按钮。
- **预估**: 6h ｜ **依赖**: T-045

---

## P2 — 运行时依赖与发布工程

### T-070 引入 tomli-w 替换手写 TOML 序列化
- **状态**: ☐ 待办
- **验证结论**: 往返一致（含反斜杠/引号转义），Python 3.9–3.14 兼容（validation §V-05）。
- **改动点**: pyproject dependencies + 重写 `save_config` + 删除 `_toml_str` + 相关测试更新。
- **预估**: 1h ｜ **依赖**: 无

### T-071 引入 platformdirs 修 Windows 配置路径 + 旧配置迁移
- **状态**: ☐ 待办
- **改动点**: `CONFIG_PATH` 改用 `platformdirs.user_config_path("harbor", appauthor=False)`；启动时检测旧路径 `~/.config/harbor/config.toml` 存在则迁移并备份；CHANGELOG 注明。
- **预估**: 2h ｜ **依赖**: 无

### T-072 setuptools-scm + PyPI Trusted Publishing
- **状态**: ☐ 待办
- **改动点**: build-system 接管版本号（tag 即发版）；`.github/workflows/release.yml`（build + pypa/gh-action-pypi-publish OIDC）；`CHANGELOG.md`。
- **预估**: 3h ｜ **依赖**: 无

---

## 建议执行批次

| 批次 | 任务 | 说明 |
|---|---|---|
| Batch 1（半天） | T-001, T-002, T-003 | 三个实锤 bug，互不依赖 |
| Batch 2（1天） | T-013, T-012, T-010, T-020 | 安全小项 + 最大性能项 |
| Batch 3（1天） | T-021, T-022, T-040, T-041 | 性能收尾 + 前端速赢 |
| Batch 4（2天） | T-030, T-031, T-032 | 架构重构（一次性做完减少冲突） |
| Batch 5（2天) | T-050, T-051, T-045, T-070, T-071 | 测试建设 + 依赖引入 |
| Batch 6（按需） | T-011, T-052, T-043, T-044, T-072 | 鉴权 + e2e + a11y + 发版 |
| Batch 7（路线图） | T-060 → T-042 → T-061 → 其余 P2 | 功能迭代 |
