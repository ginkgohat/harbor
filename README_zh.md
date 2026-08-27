# Harbor

<p align="center">
  <img src="docs/img/logo.svg" alt="Harbor" width="80" height="80">
</p>

一个本地 Web 面板，用于同时管理多个 Git 仓库。

**零依赖** —— Python 标准库 + 单个 HTML 文件。不需要 Node.js、不需要 pip 包、不需要 Docker。

<p align="center">
  <img src="docs/img/screenshot.png" alt="Harbor 截图" width="720">
</p>

## 为什么用 Harbor？

你懂那种感觉：电脑上散着 20 多个 Git 仓库——工作项目、副业项目、clone 下来读代码的开源项目……然后每个周一早上都在重复同样的操作：

> 🧐 *"上周五那个分支我推了吗？"*
> 😅 *"哦对，这个仓库已经三周没 pull 了。"*
> 😤 *"怎么这里还有未提交的改动？"*

Harbor 就是为这而生的。它是个小面板，**一眼看完所有仓库的状态**——干净、有改动、ahead、behind——还能批量 pull、stash、切换分支。不用再在十几个目录之间 cd 来 cd 去才能开始一天的工作。

它也刻意保持简单：一个 Python 文件 + 一个 HTML 文件。没有构建步骤、没有数据库、没有 Docker。两秒钟装好，然后就忘了它的存在。

## 功能

- **多仓库总览** — 一眼看清每个仓库的分支、有无未提交改动、ahead/behind 状态
- **批量 Pull** — 并发 `git pull --ff-only`，实时 SSE 进度推送
- **Stash / 丢弃 / 切 main** — 单个或批量执行常用操作
- **改动预览** — 丢弃前查看 diff，避免误操作
- **黑暗模式** — 自动跟随系统设置
- **中英文切换**
- **VS Code 集成** — 一键在 VS Code 中打开仓库
- **安全** — 仅绑定 `127.0.0.1`；危险操作需要确认

## 快速开始

> **注意**：PyPI 上的 `harbor` 是另一个项目，请从 GitHub 安装：

```bash
# 从 GitHub 安装
pip install git+https://github.com/ginkgohat/harbor.git

# 运行 — 扫描当前目录
harbor

# 或扫描指定目录
harbor ~/projects

# 或扫描多个目录
harbor ~/work ~/personal ~/oss
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，或者让 Harbor 自动打开浏览器。

### 从源码运行（开发模式）

```bash
git clone https://github.com/ginkgohat/harbor.git
cd harbor

# 方式 A：pip 开发模式安装
pip install -e .
harbor ~/projects

# 方式 B：直接运行（无需安装）
make run
# 或：PYTHONPATH=src python3 -m harbor ~/projects
```

### Makefile

```
make run      # 启动 Harbor（当前目录）
make test     # 运行测试
make lint     # 运行 lint 检查
make dev      # 开发模式安装
make clean    # 清理构建产物
```

## 配置

设置按优先级解析：**命令行参数 > 环境变量 > 配置文件 > 默认值**。

| CLI 参数 | 环境变量 | 配置项 | 默认值 |
|---|---|---|---|
| `--port` | `HARBOR_PORT` | `port` | `8765` |
| `--min-depth` | `HARBOR_MIN_DEPTH` | `min_depth` | `1` |
| `--max-depth` | `HARBOR_MAX_DEPTH` | `max_depth` | `5` |
| `--config` | `HARBOR_CONFIG` | — | `~/.config/harbor/config.toml` |
| `--no-browser` | — | — | 自动打开浏览器 |

配置文件格式为 TOML。扫描根目录以表数组的形式存储：

```toml
port = 8765
min_depth = 1
max_depth = 5

[[roots]]
path = "/Users/you/work"
label = "work"

[[roots]]
path = "/Users/you/oss"
label = "oss"
```

命令行传入的根目录（`harbor ~/work ~/oss`）会在启动时写入配置文件，
这样 Rescan 和后续运行都会继续使用它们。根目录也可以在 UI 的
Settings 面板中增删。

## 安全模型

Harbor 仅绑定 `127.0.0.1`，且**没有身份验证**——任何能本地访问你
机器的人都能到达它。对恶意网页的防护依赖两层：浏览器的同源策略，
以及服务端对 `POST /api/repo/<path>/action` 的 Origin/Referer 检查
（跨域请求返回 403）。破坏性操作（丢弃改动、切换分支）还额外需要
在 UI 中确认。请勿将 Harbor 端口转发或反向代理到不可信网络。

## 工作原理

Harbor 用 `os.walk` 扫描指定目录下的 `.git` 子目录（跨平台兼容）。对每个仓库执行 `git` 命令获取状态，以 JSON API 形式提供给前端。前端是单个 HTML 文件，通过 SSE 获取实时 pull 进度。

## 同类工具

- [ungit](https://github.com/FredrikNoren/ungit) — Web Git GUI（侧重单仓库操作）
- [gita](https://github.com/nosarthur/gita) — CLI 多仓库状态面板
- [myrepos](https://myrepos.branchable.com/) — CLI 批量仓库操作

Harbor 的独特定位：**Web UI + 批量操作 + 零依赖**。

## License

MIT