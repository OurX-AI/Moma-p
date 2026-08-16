# MomaCoder

## 项目概述

### 项目定位

Moma-Code 是一款面向软件开发场景的 **AI 编码 Agent**。它以命令行交互界面（TUI）为载体，支持开发者通过自然语言与 AI 协作完成代码编写、调试、重构、文档生成等任务。项目采用 Agent 架构，具备自主规划、工具调用、多步推理和代码库感知能力，能够理解项目上下文并执行复杂的编码工作流。

### 技术架构

| 层级 | 技术选型 | 说明 |
|------|---------|------|
| Agent 框架 | LangGraph + ReAct | 基于状态图的 Agent 编排，支持子 Agent 调度和多轮工具调用 |
| 交互界面 | Textual (TUI) | 终端富文本界面，支持实时流式输出、Slash 命令和面板布局 |
| 配置管理 | Pydantic Settings | 统一读取 `~/.moma/env`，支持类型校验和环境变量覆盖 |
| 数据持久化 | SQLite / PostgreSQL / MySQL | 通过 SQLAlchemy + Alembic 支持多数据库后端和 schema 迁移 |
| 向量存储 | Elasticsearch / OpenSearch / LanceDB | 用于代码语义检索和 Embedding 索引 |
| 图数据库 | Neo4j | 存储代码调用图和依赖关系 |
| LLM 接入 | 多 Provider 统一网关 | 支持 OpenAI、Anthropic、Google、阿里云、腾讯云、Groq、Mistral、Ollama 等 10+ 服务商 |
| 工具生态 | MCP 协议 + 自定义工具集 | 代码库分析、文件系统操作、Web 搜索、Shell 执行等 |
| 代码分析 | Tree-sitter | 支持 Go、C、C++、JavaScript 等语言的 AST 解析 |
| 打包部署 | Poetry | 标准化依赖管理和 wheel 分发 |

### 代码规模

| 指标 | 数值 |
|------|------|
| Python 源文件 | 409 个 |
| 总代码行数 | ~66,000 行 |
| 核心模块 | agents、cli、codebase、config、services、utils |
| 预置 Agent | 17 个（Coder、General、Utility 等） |
| 模型配置 | 6 类（Chat、Embedding、Rerank、TTS、STT、CV） |
| Python 版本要求 | >= 3.10, < 3.13 |

### 核心能力

- **代码库感知**：自动扫描工作区，构建符号索引和调用图，实现语义级代码检索
- **多 Agent 协作**：支持子 Agent 调度和团队协作模式，可并行处理复杂任务
- **工具调用**：内置代码分析、文件操作、Web 搜索、Shell 执行等工具，支持 MCP 协议扩展
- **会话管理**：完整的对话历史、上下文压缩和会话持久化
- **Skill 系统**：可插拔的技能模块，支持自定义工作流扩展

## 开发

配置统一读取 `~/.moma/env`（与安装态相同）。首次开发前运行一次 `moma-setup` 生成 `~/.moma/env`，或手动复制 `env.example` 到 `~/.moma/env` 后按需修改。激活虚拟环境后直接运行源码：

```powershell
.\.venv\Scripts\Activate.ps1
moma-setup                      # 首次：生成 ~/.moma/env 等运行时资源
python -m app.cli.main
```

改代码即时生效。若希望运行时数据落仓库 `data/`，可在 `~/.moma/env` 中设置 `RUNTIME_DATA_DIR=./data`（相对路径相对仓库根解析）。

如需安装后的 `moma` 命令效果，可在开发时使用 `moma --plain` 等参数直接调试：
```powershell
python -m app.cli.main --plain
python -m app.cli.main -p "你的任务"
```

## 安装

### 一键脚本安装（Linux / macOS）

```bash
curl -fsSL https://raw.githubusercontent.com/YickelFuboo/Moma-Coder/main/scripts/install.sh | bash
```

脚本自动完成：检测 Python → 创建独立虚拟环境 → 从 GitHub Releases 下载 wheel → 运行 `moma-setup` → 配置 PATH。

指定版本安装：

```bash
MOMA_VERSION=v0.1.0 curl -fsSL https://raw.githubusercontent.com/YickelFuboo/Moma-Coder/main/scripts/install.sh | bash
```

### 一键脚本安装（Windows）

在 PowerShell 中运行：

```powershell
iwr -useb https://raw.githubusercontent.com/YickelFuboo/Moma-Coder/main/scripts/install.ps1 | iex
```

脚本自动完成：检测 Python → 创建独立虚拟环境 → 从 GitHub Releases 下载 wheel → 运行 `moma-setup` → 添加到用户 PATH。安装完成后需**重新打开终端**使 PATH 生效。

指定版本安装：

```powershell
$env:MOMA_VERSION="v0.1.0"; iwr -useb https://raw.githubusercontent.com/YickelFuboo/Moma-Coder/main/scripts/install.ps1 | iex
```

### 从 GitHub Releases 手动安装

1. 前往 [Releases 页面](https://github.com/YickelFuboo/Moma-Coder/releases) 下载最新 `.whl` 文件
2. 安装到虚拟环境并初始化：

```bash
python -m venv ~/.moma/venv
~/.moma/venv/bin/pip install momacoder-*.whl
~/.moma/venv/bin/moma-setup
```

3. 将 `~/.moma/venv/bin` 加入 PATH，或创建符号链接到 `~/.local/bin`

### 从源码安装（部署）

代码拷贝到 site-packages，与仓库独立，repo 改动不影响已安装的稳定版本：

```powershell
cd F:\MOMA\Moma-Coder
.\.venv\Scripts\Activate.ps1
python -m pip install .   # 不用 -e，基线副本安装到 site-packages
moma-setup
```

说明：已激活 venv 时不要加 `--user`，否则会报 User site-packages are not visible。

`moma-setup`（或 `python -m app.install`）会把 `agents` / `skills` / `models` 同步到 `~/.moma`；若尚无 `~/.moma/env` 则从 `env.example` 复制一份。

### 启动服务

安装后直接运行，配置统一读取 `~/.moma/env`，无需额外环境变量：

```powershell
moma
```

| 场景 | 启动方式 | env 来源 |
|------|----------|----------|
| 开发 | `python -m app.cli.main` | `~/.moma/env` |
| 部署 | `moma` | `~/.moma/env` |

### 命令

MOMA 启动后会自动在后台扫描 workspace 触发 CodeBase 预分析（索引代码符号、构建调用图、准备语义检索）。可通过以下命令查看进度或强制重扫。

**CLI 子命令**

```powershell
moma codebase            # 查看当前状态（等价于 status）
moma codebase status     # 同上
moma codebase rescan     # 强制重新扫描
```

`moma codebase` 仅启动 DB/scheduler，不走 Agent/TUI 流程，适合在脚本里查状态或定时触发重扫。

**TUI slash 命令**

在 `moma` 交互界面输入：

```
/codebase            # 输出多行状态报告到聊天区
/codebase rescan     # 触发重扫，并立刻显示新状态
```

**实时进度面板**

TUI 启动后会每 2 秒轮询一次扫描状态：

- 扫描进行中：welcome-box 右侧自动从 "Recent activity" 切换为 "CodeBase 进度" 卡片，显示 `running (3s ago) · 217/350 emb` 等进度信息
- welcome-box 隐藏后（首条消息后），底部 status-row 左侧仍保留一行指示器 `· CB ⟳ 217/350 emb`
- 扫描结束：面板切回 "Recent activity"，指示器消失

### 更新

代码更新后重新安装即可：

```powershell
cd F:\MOMA\Moma-Coder
.\.venv\Scripts\Activate.ps1
python -m pip install .      # 覆盖旧版本
moma-setup             # 同步可能有更新的 agents/skills/models
```

工作区默认 = 当前目录。

若提示找不到命令，确认已激活 venv，或把 `.venv\Scripts` 加入 PATH。

### 卸载

卸载：`pip uninstall MomaCoder`
