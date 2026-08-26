# 可行性深度评估报告（独立评审）

> 评审角色：未参与前期规划的第二评审人，仅依据 `improvement-tasks.md` 任务清单 + 当前源码 + 实证实验进行证伪式评估。
> 评估日期：与 tasks 文档同批。
> 结论速览：**31 个任务中 29 个直接可行，2 个需修订方案（T-011、T-022），另发现 3 个清单遗漏项（新增 T-080~T-082）。**

---

## 一、逐任务可行性裁定

### P0 Bug 修复

| 任务 | 裁定 | 依据 |
|---|---|---|
| T-001 worktree 漏检 | ✅ **可行，已实证** | 实验 V-01：worktree 的 `.git` 为 ASCII 文件，当前 scanner 确实漏检；修复方案一行代码成立 |
| T-002 JOBS 泄漏 | ✅ **可行，已实证** | 实验 V-02：job 创建后 done 事件滞留队列，无任何清理路径；TTL 方案无侵入性风险 |
| T-003 i18n 补齐 | ✅ **可行** | 三处硬编码行号已精确定位；grep 防回归脚本技术上平凡 |

### ⚠️ 发现的新边界：T-001 的连带效应（→ 新增 T-080）

实验 V-08：修复后，含**损坏 `.git` 文件**（如 `gitdir:` 指向不存在路径）的目录也会被收录。实测 `repo_status()` 对此类 repo 会优雅降级（run_git 返回非零 rc，branch 为空、detached=true），**不会崩溃**——但 UI 会显示一个永远 detached 的"幽灵卡片"。建议：
1. T-001 验收标准追加一条：`.git` 文件指向不存在路径时不收录（scanner 里加一次 `os.path.exists(gitdir_target)` 校验），或收录但状态标注为 broken；
2. 补降级路径单测。

### P1 安全

| 任务 | 裁定 | 依据 |
|---|---|---|
| T-010 origin 收口 | ✅ 可行 | `_dispatch` 是唯一分发点，收口改动面小；现有 `_check_origin` 逻辑可直接复用 |
| T-011 token 鉴权 | ⚠️ **需修订** | 见下方专项分析 |
| T-012 CSP | ✅ 可行 | 单文件内联脚本/style 已确认需要 `unsafe-inline`；connect-src 'self' 与 EventSource/fetch 用法兼容 |
| T-013 禁用凭据提示 | ✅ 可行 | `GIT_TERMINAL_PROMPT=0` 是 git 官方机制；注意 `GCM_INTERACTIVE` 仅对 Windows Git Credential Manager 生效，属尽力而为，文档里别承诺过度 |

#### T-011 专项分析（关键发现）

前端存在 **`new EventSource(...)`**（index.html L967）——EventSource API **无法设置自定义 header**。因此原方案的"header 校验"不可行，必须二选一：

- **方案 A（推荐）**：token 走 query string。页面 URL 自带 `?token=`，JS 启动时读 `location.search` 并统一拼接到所有 `fetch` 和 `EventSource` URL。改动集中在一个工具函数，约 10 行前端改动。
- 方案 B：首次带 token 访问时 Set-Cookie（HttpOnly=false，SameSite=Strict），后续请求自动携带。更优雅，但引入 cookie 语义，且 SameSite=Strict 下从浏览器地址栏直接打开仍会带 cookie（同站），行为正确。

**裁定**：采用方案 A，任务描述需更新；预估工时 3h → 4h。另注意 SSE 自动重连（前端有 retries 逻辑）时 token 必须在重连 URL 中保持——方案 A 天然满足。

### P1 性能

| 任务 | 裁定 | 依据 |
|---|---|---|
| T-020 porcelain=v2 合并 | ✅ **可行，已实证** | 实验 V-04/V-07：单命令给出 branch/dirty/ahead(`# branch.ab +2 -0`)/behind/untracked；git ≥ 2.11（2016年）支持，CI 全矩阵安全 |
| T-021 缓存默认分支 | ✅ 可行 | 依赖关系合理（同函数改造） |
| T-022 前端增量刷新 | ⚠️ 小幅修订 | 见下 |

#### T-022 边界确认

`do_action` 现返回 `(code, {"ok", "output"})`——要带回最新 status，action 处理完后需再跑一次 `repo_status`（+1 子进程/次点击，相比省掉的全量 N×6 仍是数量级改善，值得做）。但注意：**discard/stash 后 repo 状态变化剧烈**，status 必须在动作完成后采样而非之前。建议实现顺序：action → `repo_status` → 合并响应。预估 3h 维持不变。

#### T-020 解析器必须处理的四个分支形态（实验确认）

| 场景 | 输出特征 | 解析要求 |
|---|---|---|
| 正常分支 | `# branch.head main` + `# branch.ab +N -M` | 常规解析 |
| unborn branch（空仓库） | `# branch.oid (initial)`，**无 branch.ab 行** | ahead/behind = None |
| detached HEAD | `# branch.head (detached)` | detached=True, branch="" |
| 无 upstream | 无 `# branch.ab` 行 | ahead/behind = None |

验收标准应补充这四条形态的解析器单测（原清单只提了部分）。

### P1 架构重构

| 任务 | 裁定 | 依据 |
|---|---|---|
| T-030 AppState | ✅ 可行，工时偏紧 | grep 显示 test_server.py 有 **25 处** `server.Handler.` 引用，重构波及面比预想大；4h → 5h |
| T-031 去 HTTP 化 + dataclass | ✅ 可行 | `do_action` 现签名已确认返回 HTTP code；拆分方向清晰。建议 dataclass 用 stdlib `dataclasses`，不要引 pydantic（守住运行时零依赖原则） |
| T-032 lint/mypy 加码 | ✅ 可行 | ruff UP 规则在 py39 target 下会提示 tomllib 条件导入改写，需保留 `python_version < '3.11'` 分支——UP 规则对此是安全的（它按 target-version 判断） |

### P1 前端 / 测试 / P2

| 任务 | 裁定 | 备注 |
|---|---|---|
| T-040 ahead 徽章 | ✅ 可行 | 数据已在 API 中，纯前端 |
| T-041 diff 截断 | ✅ 可行 | 服务端截断点放在 `get_diff`，512KB 合理 |
| T-042 最近提交信息 | ✅ 可行 | `%cs` 需 git ≥ 2.15（2017），矩阵安全；空 repo（unborn）需容错 |
| T-043 a11y | ✅ 可行 | 标准模式，无技术风险 |
| T-044 暗色三态 | ✅ 可行 | CSS 变量结构现成，`data-theme` 覆盖即可；注意 `prefers-color-scheme` media query 需改为 `[data-theme="dark"]` 选择器双轨 |
| T-045 前端抽测 | ✅ 可行 | Node 直接 require 抽出的纯函数模块可行；STR 一致性断言可防 T-003 回归 |
| T-050 SSE 测试 | ✅ 可行 | ThreadingHTTPServer + urllib 读流是标准做法 |
| T-051 hypothesis | ✅ 可行 | 不变式选择恰当 |
| T-052 Playwright | ⚠️ 依赖正确 | 已正确标注依赖 T-011（token 确定后才能写 e2e）；Windows 上 playwright 安装偶发慢，CI 单独 job 的设计正确 |
| T-053 demo | ✅ 平凡可行 | — |
| T-060 Fetch All | ✅ 可行 | job/SSE 机制完全复用；fetch 比 pull 快且无合并冲突风险，是低风险高价值功能 |
| T-061~064 | ✅ 方向可行 | 属路线图，实施前需各自细化，本轮不深评 |
| T-070 tomli-w | ✅ **可行，已实证** | 实验 V-05：往返一致（反斜杠/引号转义正确）；1.2.0 版本 Requires 为空（零传递依赖）；纯 Python，py3.9+ 兼容声明属实 |
| T-071 platformdirs | ✅ **可行，已实证** | 4.11.4 零依赖；macOS 实测路径正确；**迁移逻辑必须做**（老用户配置在 `~/.config/harbor/`），任务描述已含 |
| T-072 setuptools-scm | ✅ 可行 | 注意 dist/ 下已有手工构建产物，切换后应清理并在 .gitignore 排除 |

---

## 二、清单遗漏项（本次评审新增）

### T-080（新）损坏 `.git` 文件的防护与降级测试
- 由 T-001 引出：`.git` 文件存在但 `gitdir` 目标失效时的 scanner 校验 + 幽灵卡片的 UI 表现。
- **建议并入 T-001 验收标准**，或作为其子任务。

### T-081（新）并发 rescan 与 action 的竞态
- 评审中发现：`_rescan()` 直接整体替换 `Handler.repos` 字典引用。若用户在 Settings 删除 root 的同时批量操作正在遍历旧 repos 快照，行为是"对已移除 repo 执行动作"（do_action 会因 repos.get(path) 为 None 返回 404，**不会误伤磁盘**）——安全性 OK，但 UI 可能出现瞬时 404 报错。
- 建议：P2 低优先级记录即可，前端对 404 做"repo 已被移除"的友好提示。

### T-082（新）Python 版本矩阵与本地环境差异
- 本地 `.venv` 为 Python 3.14，CI 矩阵上限 3.13。tomli-w/platformdirs 在 3.14 实测正常，但建议 CI 矩阵补 `"3.14"` 或加 `allow-prerelease` 策略说明，避免"本地绿、CI 未验"的盲区。

---

## 三、总体结论

1. **全部 31 项任务无一被否决**；前期评审的技术判断经实证检验基本准确。
2. **2 项需修订**：
   - T-011：EventSource 无法带头 → token 改走 query string（方案 A），工时 +1h；
   - T-020：解析器需显式覆盖 unborn/detached/no-upstream 三种非常规形态。
3. **3 项遗漏**（T-080~082）：均为小项，其中 T-080 应立即并入 T-001。
4. **工时修正**：总预估从 ~75h 调整为 ~78h，主要来自 T-011(+1h)、T-030(+1h)、T-080(+0.5h)。
5. **批次计划无需调整**：Batch 1（三个实锤 bug）可以立刻开工，互不依赖、风险最低、收益立现。

## 附：实证实验索引

| 编号 | 实验 | 结果 |
|---|---|---|
| V-01 | worktree 目录扫描 | ❌ 漏检复现（`.git` 为 68 字节文本文件） |
| V-02 | JOBS 清理机制检查 | ❌ 无任何清理路径，done 事件滞留队列 |
| V-03 | i18n 硬编码 grep | L794/L501「全部」+ Settings 英文文案 |
| V-04/V-07 | porcelain=v2 输出分析 | 单命令含 dirty/ahead/behind/detached/unborn 全信息 |
| V-05 | tomli-w 往返测试 | 特殊字符转义正确，零传递依赖 |
| V-06 | CSP / origin 覆盖 grep | CSP 未设置；origin 仅 action 路由 1 处调用 |
| V-08 | 损坏 .git 文件行为 | 修复后会被收录，repo_status 优雅降级不崩溃 |
