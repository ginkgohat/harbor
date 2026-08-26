# Harbor 改进任务拆解

> 来源：2024-08 项目全面评审（架构 / 代码 / 安全 / 性能 / 前端 / 测试 / 发布）。
> 每个任务是**可独立开发、独立验收**的最小交付单元。
> 可行性验证结论见同目录 `feasibility-validation.md`。
> 已完成的任务（P0 Bug、安全小项、性能、前端速赢 i18n/CSP/ahead/diff 截断/增量刷新/porcelain=v2 合并/default 分支缓存/worktree 漏检/JOBS 泄漏、ruff 加码+mypy+pre-commit、SSE 集成测试、setuptools-scm+Trusted Publishing、CI 3.14 矩阵）已从本清单移除，详见已合并的 PR。

## 状态图例

`☐ 待办` · `🔄 进行中` · `✅ 完成` · `❌ 已否决`

---

## P1 — 安全加固

### T-011 本地 token 鉴权（防 CSRF / DNS rebinding / 本地恶意进程）
- **状态**: ☐ 待办
- **问题**: discard 是不可逆数据破坏操作，当前任何本地进程或 rebinding 页面都可调用。
- **方案**（已修订，见 feasibility-validation §T-011）：启动生成 `secrets.token_urlsafe(16)`；服务 URL 为 `http://127.0.0.1:<port>/?token=<hex>`；前端从 `location.search` 读 token 后拼到所有 `fetch` 和 `EventSource` URL；中间件校验 query；token 不匹配返回 403。静态资源放行。
- **验收标准**:
  - [ ] 无 token 访问 API 返回 403
  - [ ] 带 token 的完整用户流程（浏览器手工 + e2e）正常
  - [ ] token 不写入服务器访问日志，README 提醒用户 URL 会进入浏览器历史
  - [ ] README 更新说明
- **预估**: 4h ｜ **依赖**: 无

---

## P1 — 架构重构

### T-030 AppState 替代 Handler 类属性全局状态
- **状态**: ☐ 待办
- **问题**: `repos/min_depth/max_depth/cli_*` 全挂类属性，测试被迫写快照 fixture 防串扰。
- **改动点**: 引入 `AppState` dataclass；`__main__` 构造注入 handler；删除 `_restore_handler_state` fixture。
- **验收标准**: 测试直接构造 AppState 实例；fixture 删除后测试套件稳定。
- **预估**: 5h ｜ **依赖**: 无

### T-031 git 层去 HTTP 化 + Repo dataclass + safe_pull 去重
- **状态**: ☐ 待办
- **改动点**:
  - `do_action` 返回领域结果 `ActionOutcome(ok, output)`，HTTP 映射移入 server
  - `Repo` dataclass 替代裸 dict（`asdict()` 序列化给前端）
  - 抽取 `safe_pull(path)` 合并 `pull_one` 与 `do_action("pull")` 的重复跳过逻辑
- **验收标准**: `harbor.git` 模块 import 不含 http 概念；重复逻辑消除且行为不变。
- **预估**: 4h ｜ **依赖**: T-030

### T-032 Ruff 规则加码 + 渐进式 mypy
- **状态**: ✅ 完成
- **改动点**: ruff select 追加 `UP, SIM, RUF, C4` 并修复告警；mypy 先只查 `src/harbor`、宽松配置起步；加 `.pre-commit-config.yaml`（ruff + ruff-format + 基础 hooks）。
- **验收标准**: `make lint` 含新规则全绿；pre-commit 本地可用；CI 加 pre-commit job（可选）。
- **预估**: 2h ｜ **依赖**: 建议 T-031 之后（减少返工）

---

## P1 — 前端 / UI

### T-042 卡片显示最近提交信息
- **状态**: ☐ 待办
- **改动点**: `repo_status` 增加 `git log -1 --format=%cs %s`（独立子进程）；卡片展示相对时间 + message。
- **验收标准**: 有 commit / 空 repo（unborn branch）均正常显示。
- **预估**: 2h ｜ **依赖**: 无

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
- **状态**: ✅ 完成
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
- **预估**: 4h ｜ **依赖**: 无（status 数据已实时）

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
- **状态**: ✅ 完成
- **改动点**: build-system 接管版本号（tag 即发版）；`.github/workflows/release.yml`（build + pypa/gh-action-pypi-publish OIDC）；`CHANGELOG.md`。
- **预估**: 3h ｜ **依赖**: 无

---

## 清单遗漏项（来自可行性评审）

### T-081 并发 rescan 与 action 的竞态
- **状态**: ☐ 待办（P2 低优先级）
- **说明**: `_rescan()` 整体替换 `repos` 引用。用户在 Settings 删除 root 的同时批量操作仍在遍历旧 repos 快照——安全性 OK（do_action 因 repos.get 为 None 返回 404，不会误伤磁盘），但 UI 可能出现瞬时 404 报错。
- **建议**: 前端对 404 做「repo 已被移除」的友好提示。

### T-082 Python 版本矩阵与本地环境差异
- **状态**: ✅ 完成
- **说明**: 本地 `.venv` 为 Python 3.14，CI 矩阵上限 3.13。tomli-w/platformdirs 在 3.14 实测正常，但建议 CI 矩阵补 `"3.14"` 或加 `allow-prerelease` 策略说明。

---

## 建议执行批次

| 批次 | 任务 | 说明 |
|---|---|---|
| Batch 4（2天） | T-030, T-031 | 架构重构（一次性做完减少冲突） |
| Batch 5（2天) | T-051, T-045, T-070, T-071 | 测试建设 + 依赖引入 |
| Batch 6（按需） | T-011, T-052, T-043, T-044 | 鉴权 + e2e + a11y |
| Batch 7（路线图） | T-060 → T-042 → T-061 → 其余 P2 | 功能迭代 |
