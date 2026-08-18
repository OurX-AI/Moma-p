# MomaCoder

> AI 驱动的编码 Agent，让终端成为你的智能开发伙伴

[![Python Version](https://img.shields.io/badge/Python-3.10+-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/OurX-AI/Moma-p.svg)](https://github.com/OurX-AI/Moma-p/releases)

MomaCoder 是一款面向软件开发场景的 AI 编码 Agent，以命令行交互界面（TUI）为载体，支持开发者通过自然语言与 AI 协作完成代码编写、调试、重构、文档生成等任务。

## ✨ 功能特性

- **代码库感知** — 自动扫描工作区，构建符号索引和调用图，实现语义级代码检索
- **多 Agent 协作** — 支持子 Agent 调度和团队协作模式，可并行处理复杂任务
- **工具调用** — 内置代码分析、文件操作、Web 搜索、Shell 执行等工具，支持 MCP 协议扩展
- **会话管理** — 完整的对话历史、上下文压缩和会话持久化
- **Skill 系统** — 可插拔的技能模块，支持自定义工作流扩展
- **多模型支持** — OpenAI、Anthropic、Google、DeepSeek、通义千问、Ollama 等 10+ 服务商

## 📸 演示

<!-- TODO: 添加终端界面截图或 GIF -->

## 🚀 快速开始

### 安装

#### 一键脚本（推荐）

Linux / macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.sh | bash
```

Windows:

```powershell
$script = "$env:TEMP\moma_install.ps1"; iwr -useb https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.ps1 -OutFile $script; & $script; Remove-Item $script -Force
```

指定版本：

Linux / macOS:

```bash
MOMA_VERSION=v0.1.0 curl -fsSL https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.sh | bash
```

Windows:

```powershell
$env:MOMA_VERSION="v0.1.0"; iwr -useb https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.ps1 | iex
```

> 安装完成后请重新打开终端使 PATH 生效

#### Windows 可执行文件

从 [Releases](https://github.com/OurX-AI/Moma-p/releases) 下载 `momacoder.exe`，放到任意目录后直接运行：

```powershell
.\momacoder.exe
```

> 此方式无需安装 Python，开箱即用。

#### 手动安装

1. 从 [Releases](https://github.com/OurX-AI/Moma-p/releases) 下载 `.whl` 文件
2. 安装并初始化（在任意目录执行，`.whl` 路径替换为实际下载路径）：

```bash
python -m venv ~/.moma/venv
~/.moma/venv/bin/pip install ~/Downloads/momacoder-*.whl
~/.moma/venv/bin/moma-setup
```

3. 将虚拟环境可执行目录加入 PATH：

   Linux / macOS：

   ```bash
   echo 'export PATH="$HOME/.moma/venv/bin:$PATH"' >> ~/.bashrc
   source ~/.bashrc
   ```

   Windows（PowerShell）：

   ```powershell
   [Environment]::SetEnvironmentVariable("Path", "$env:USERPROFILE\.moma\venv\Scripts;" + [Environment]::GetEnvironmentVariable("Path", "User"), "User")
   ```

   Windows（PowerShell）：

   ```powershell
   [Environment]::SetEnvironmentVariable("Path", "$env:USERPROFILE\.moma\venv\Scripts;" + [Environment]::GetEnvironmentVariable("Path", "User"), "User")
   ```

#### 从源码安装

Linux / macOS:

```bash
git clone https://github.com/OurX-AI/Moma-p.git
cd Moma
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/moma-setup
```

Windows:

```powershell
git clone https://github.com/OurX-AI/Moma-p.git
cd Moma
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\moma-setup
```

安装后需将虚拟环境的可执行目录加入 PATH，以便在任意位置使用 `moma` 命令：

| 系统 | 路径 |
|------|------|
| Linux / macOS | `<Moma目录>/.venv/bin` |
| Windows | `<Moma目录>\.venv\Scripts` |

也可以不配置 PATH，直接在项目目录下通过 `.venv/bin/moma`（或 `.venv\Scripts\moma`）启动。


### ⚙️ 配置

#### 模型配置

`~/.moma/models/` 目录下包含以下配置文件：

| 文件 | 用途 | 必要性 |
|------|------|--------|
| `chat_models.json` | 聊天模型 | 必配 |
| `embedding_models.json` | Embedding 模型（语义检索） | 必配 |
| `rerank_models.json` | Rerank 模型 | 可选 |
| `tts_models.json` | 语音合成模型 | 可选 |
| `stt_models.json` | 语音识别模型 | 可选 |
| `cv_models.json` | 视觉模型 | 可选 |

> 首次安装后 `moma-setup` 会自动复制示例配置文件到 `~/.moma/models/`

#### 搜索服务配置（可选）

编辑 `~/.moma/env`

1. 配置 Web 搜索 API Key：

```env
# Tavily 搜索
TAVILY_API_KEY=tvly-xxxx

# 或 Serper 搜索（Google SERP，2500 次免费）
SERPER_API_KEY=xxxx

# 或使用免费的 DuckDuckGo（无需配置）
WEB_SEARCH_PRIMARY=duckduckgo
```

2. 按照需要配置各种全局功能开关（可选）

### 启动

```bash
moma
```

首次启动会自动扫描工作区构建代码索引，耐心等待即可。


## 📖 使用说明

### 命令行参数

```bash
moma                              # 启动交互式 TUI
moma -p "写一个快速排序函数"        # 直接执行任务
moma --plain                      # 纯文本 REPL 模式
moma --resume <session_id>        # 恢复历史会话
moma --model deepseek/deepseek-chat  # 指定模型
moma -w /path/to/project          # 指定工作区目录
```

### CodeBase 管理

```bash
moma codebase status              # 查看代码库扫描状态
moma codebase rescan              # 强制重新扫描
moma codebase experience          # 查看经验提取状态
```

TUI 内 Slash 命令：

```
/codebase                         # 输出状态报告
/codebase rescan                  # 触发重扫
```

### 更新与卸载

**一键脚本 / 手动安装（whl）：**

```bash
pip install --upgrade MomaCoder   # 更新
pip uninstall MomaCoder           # 卸载
```

**从源码安装：**

```bash
# 更新：进入项目目录重新安装
cd Moma
git pull
.venv/bin/pip install -e .        # Linux / macOS
.venv\Scripts\pip install -e .    # Windows

# 卸载
rm -rf ~/.moma                    # 删除运行时数据
rm -rf .venv                      # 删除虚拟环境（或在 Windows 中 Remove-Item -Recurse -Force .venv）
# 从 PATH 中移除 .venv/bin（或 .venv\Scripts）
```

## 🛠️ 开发

配置统一读取 `~/.moma/env`。首次开发前运行一次 `moma-setup`：

```bash
moma-setup
python -m app.cli.main
```

改代码即时生效。

## 🤝 贡献

欢迎贡献！请参考以下步骤：

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/amazing-feature`
3. 提交更改：`git commit -m 'Add amazing feature'`
4. 推送到分支：`git push origin feature/amazing-feature`
5. 提交 Pull Request

## 🔧 技术栈

<details>
<summary>点击展开</summary>

| 层级 | 技术 |
|------|------|
| Agent 框架 | LangGraph + ReAct |
| 交互界面 | Textual (TUI) |
| 配置管理 | Pydantic Settings |
| 数据库 | SQLite / PostgreSQL / MySQL |
| 向量存储 | Elasticsearch / OpenSearch / LanceDB |
| 图数据库 | Neo4j |
| LLM 接入 | 多 Provider 统一网关 |
| 代码分析 | Tree-sitter |
| 打包部署 | Poetry |

</details>

## 📄 许可证

[GNU General Public License v3.0](LICENSE)
