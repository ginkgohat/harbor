# Harbor

<p align="center">
  <img src="docs/img/logo.svg" alt="Harbor" width="80" height="80">
</p>

一个本地 Web 面板，用于同时管理多个 Git 仓库。

**近零依赖** —— 纯 Python 后端 + 自包含浏览器前端。不需要 Node.js、不需要 Docker、没有构建步骤。仅有的三个运行时 pip 包都是小巧且按条件引入的辅助库：`tomli`（仅 Python < 3.11）、`tomli-w` 和 `platformdirs`。

<p align="center">
  <img src="docs/img/screenshot.png" alt="Harbor 截图" width="720">
</p>

## 为什么用 Harbor？

你懂那种感觉：电脑上散着 20 多个 Git 仓库——工作项目、副业项目、clone 下来读代码的开源项目……然后每个周一早上都在重复同样的操作：

> 🧐 *"上周五那个分支我推了吗？"*
> 😅 *"哦对，这个仓库已经三周没 pull 了。"*
> 😤 *"怎么这里还有未提交的改动？"*

Harbor 就是为这而生的。它是个小面板，**一眼看完所有仓库的状态**——干净、有改动、ahead、behind——还能批量 pull、stash、切换分支。不用再在十几个目录之间 cd 来 cd 去才能开始一天的工作。

它也刻意保持简单：一个 Python 包 + 一个自包含的 HTML 前端。没有构建步骤、没有数据库、没有 Docker。两秒钟装好，然后就忘了它的存在。

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

> **注意**：PyPI 上的 `harbor` 是另一个项目，请从 GitHub 安装。

### 用 pipx 安装（推荐）

[pipx](https://pipx.pypa.io/) 会为 Harbor 创建独立的虚拟环境，并把 `harbor`
命令放进 `PATH`。它能绕开 Homebrew、Debian、Ubuntu、Fedora 等 Python 上
`pip` 必然遇到的 `error: externally-managed-environment`（PEP 668），也不会
弄坏你其他的 Python 包。

```bash
# 1. 安装 pipx（如果 `pipx --version` 已经可用则跳过）
brew install pipx                    # macOS（Homebrew）
sudo apt install pipx                # Debian / Ubuntu 23.04+
python3 -m pip install --user pipx   # 备选：允许用户级安装的 Python

# 2. 把 pipx 的安装目录 ~/.local/bin 加入 PATH
pipx ensurepath                      # 之后请打开新的终端

# 3. 从 GitHub 安装 Harbor
pipx install "git+https://github.com/ginkgohat/harbor.git"

# 4. 验证
harbor --version                     # → harbor 0.1.2.dev9+g...
```

### 装进独立的 virtualenv（备选）

如果你希望自己管理一个独立环境，可以用这种方式：

```bash
python3 -m venv ~/.venvs/harbor
~/.venvs/harbor/bin/pip install "git+https://github.com/ginkgohat/harbor.git"
~/.venvs/harbor/bin/harbor --version
```

Windows 上的可执行文件在 `~/.venvs/harbor/Scripts/`，不是 `bin/`。

### 更新 / 卸载

```bash
# pipx 安装
pipx upgrade harbor        # 强制从同一 GitHub URL 重新构建：pipx reinstall harbor
pipx uninstall harbor

# virtualenv 安装
~/.venvs/harbor/bin/pip install --upgrade "git+https://github.com/ginkgohat/harbor.git"
rm -rf ~/.venvs/harbor
```

Harbor 也内置了检查安装方式并给出正确命令的辅助子命令：

```bash
harbor update       # 升级，或提示对应的 pipx / venv / PEP 668 做法
harbor uninstall    # 卸载，或提示对应的 pipx / PEP 668 做法
```

> **直接用 `pip` 的限制** —— `pip install ...` 和
> `pip install --upgrade ...` 只适用于 virtualenv 内部，或者在不受 PEP 668
> 管理的 Python 上加 `--user`。在 Homebrew/Debian/Fedora 的系统 Python 上会
> 报 `error: externally-managed-environment`。逃生用法
> `pip install --user --break-system-packages "git+..."` 确实能装，但可能弄坏
> 系统/Homebrew 提供的 Python 包 —— 请优先用 pipx 或 virtualenv。

```bash
# 运行 — 扫描当前目录
harbor

# 或扫描指定目录
harbor ~/projects

# 或扫描多个目录
harbor ~/work ~/personal ~/oss
```

### 后台运行

```bash
harbor start        # 后台启动 Harbor
harbor status       # 查看 Harbor 运行状态
harbor stop         # 停止后台运行的 Harbor
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，或者让 Harbor 自动打开浏览器。

### 从源码运行（开发模式）

```bash
git clone https://github.com/ginkgohat/harbor.git
cd harbor

# 方式 A：editable 安装 + 开发依赖
# （需要时 `make dev` 会加上 --break-system-packages）
make dev
harbor ~/projects

# 方式 B：直接运行（无需安装）
make run
# 或：PYTHONPATH=src python3 -m harbor ~/projects
```

editable 安装就是一份源码 checkout，无法自更新 —— `harbor update` 会明确
告诉你。更新开发用的 checkout 请用 `git pull`（依赖有变化时再 `make dev`）。

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

## 故障排查

### `error: externally-managed-environment`

你的 Python 被 Homebrew、Debian、Ubuntu、Fedora 等标记为“外部管理”（PEP 668），
pip 拒绝往里安装。请改用 pipx（推荐）或 virtualenv：

```bash
pipx install "git+https://github.com/ginkgohat/harbor.git"
# 或
python3 -m venv ~/.venvs/harbor
~/.venvs/harbor/bin/pip install "git+https://github.com/ginkgohat/harbor.git"
```

这些 Python 上单加 `--user` 没用；`--break-system-packages` 能用，但可能弄坏
系统提供的包。`harbor update` 会自动识别这种情况并打印上面的选项，而不是让
pip 抛错。

### `harbor: command not found`

pipx 和 `pip install --user` 会把可执行文件放在 `~/.local/bin`，它不一定在
`PATH` 里：

```bash
pipx ensurepath                                    # 然后打开新的终端
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # 非 pipx 安装
```

不依赖 `PATH` 地检查装了什么、装在哪：

```bash
pipx list                       # pipx 管理的应用及其可执行文件
which -a harbor                 # PATH 上所有 harbor，按解析顺序
~/.local/bin/harbor --version   # 绕过 PATH 直接执行
```

### 装了/升级了，但版本或行为没变

安装、更新、卸载必须作用于**提供 `harbor` 命令的同一个解释器**。一台机器上可能
有多个 Python 和 pip：

```bash
which -a python3 pip harbor     # 注意顺序，第一个生效
harbor --version                # 你的 shell 实际运行的版本
python3 -m pip show harbor      # 这个解释器里装的版本
/opt/homebrew/bin/harbor --version   # 另一份安装报告的版本
```

如果 `pip install --upgrade ...` 改的是另一个 Python，命令就会一直运行旧副本。
可靠的做法是用 pipx（每个应用一个隔离环境）或 virtualenv 方式，始终调用
`~/.venvs/harbor/bin/pip` 和 `~/.venvs/harbor/bin/harbor`。清理时请从每个
解释器里分别卸载（`pipx uninstall harbor`、`pip uninstall harbor`……），直到
`which -a harbor` 为空。

### `harbor update` 提示不支持 editable 安装

editable 安装（`make dev`、`pip install -e .`）本身就是一份源码 checkout，pip
没有可升级的东西。请更新这份 checkout：

```bash
git -C /path/to/harbor pull
make dev
```

### 从 GitHub 安装后版本号反而更低

Harbor 的版本号由 `setuptools-scm` 根据 pip 构建的那份 clone 里的 git tag 推导。
如果 tag 没有推到远端，GitHub 构建出的版本就会低于本地 checkout —— 例如本地是
`0.1.2.dev9`，从 GitHub 装出来却是 `0.1.1.dev15`，因为 `v0.1.1` 这个 tag 只在
本地存在：

```bash
git tag -l                       # 本地 clone 的 tag
git ls-remote --tags origin      # GitHub 上真实存在的 tag
git push --tags                  # 把缺失的 tag 推上去
harbor --version                 # 已安装副本的版本
```

`pip install --upgrade git+...` 会从默认分支重新构建，所以它确实可能把版本号
“降级”。这只是版本号字符串造成的错觉，不代表装的是旧代码：`harbor --version`
里带有 `+g<sha>` 提交号，可以和 `git rev-parse HEAD` 对比确认。

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