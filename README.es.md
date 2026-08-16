# MomaCoder

## Overview

### What is MomaCoder

MomaCoder is an **AI Coding Agent** designed for software development workflows. It provides a terminal-based user interface (TUI) that enables developers to collaborate with AI on code writing, debugging, refactoring, and documentation generation using natural language. Built on an Agent architecture, it features autonomous planning, tool invocation, multi-step reasoning, and codebase awareness — capable of understanding project context and executing complex coding workflows.

### Technology Stack

| Layer | Technology | Description |
|-------|-----------|-------------|
| Agent Framework | LangGraph + ReAct | State-graph-based Agent orchestration with sub-agent scheduling and multi-turn tool invocation |
| User Interface | Textual (TUI) | Rich terminal UI with real-time streaming output, slash commands, and panel layouts |
| Configuration | Pydantic Settings | Unified config from `~/.moma/env` with type validation and environment variable override |
| Data Persistence | SQLite / PostgreSQL / MySQL | Multi-database backend support via SQLAlchemy + Alembic with schema migrations |
| Vector Storage | Elasticsearch / OpenSearch / LanceDB | Semantic code retrieval and embedding indexing |
| Graph Database | Neo4j | Code call graphs and dependency relationships |
| LLM Gateway | Multi-provider unified gateway | OpenAI, Anthropic, Google, Alibaba Cloud, Tencent Cloud, Groq, Mistral, Ollama, and 10+ providers |
| Tool Ecosystem | MCP protocol + custom tools | Codebase analysis, file system operations, web search, shell execution, etc. |
| Code Analysis | Tree-sitter | AST parsing for Go, C, C++, JavaScript, and more |
| Packaging | Poetry | Standardized dependency management and wheel distribution |

### Codebase Scale

| Metric | Value |
|--------|-------|
| Python source files | 409 |
| Total lines of code | ~66,000 |
| Core modules | agents, cli, codebase, config, services, utils |
| Pre-built Agents | 17 (Coder, General, Utility, etc.) |
| Model configurations | 6 types (Chat, Embedding, Rerank, TTS, STT, CV) |
| Python version requirement | >= 3.10, < 3.13 |

### Core Capabilities

- **Codebase Awareness**: Automatically scans workspaces, builds symbol indexes and call graphs for semantic-level code retrieval
- **Multi-Agent Collaboration**: Sub-agent scheduling and team collaboration modes for parallel processing of complex tasks
- **Tool Invocation**: Built-in code analysis, file operations, web search, shell execution, and MCP protocol extensibility
- **Session Management**: Complete conversation history, context compression, and session persistence
- **Skill System**: Pluggable skill modules with custom workflow extensibility

## Development

All configuration is read from `~/.moma/env` (same as installed mode). Before first development, run `moma-setup` once to generate `~/.moma/env`, or manually copy `env.example` to `~/.moma/env` and edit as needed. After activating the virtual environment, run the source code directly:

```powershell
.\.venv\Scripts\Activate.ps1
moma-setup                      # First time: generate ~/.moma/env and runtime resources
python -m app.cli.main
```

Code changes take effect immediately. To store runtime data in the repo's `data/` directory, set `RUNTIME_DATA_DIR=./data` in `~/.moma/env` (relative paths resolve from the repo root).

To debug with the installed `moma` command behavior:

```powershell
python -m app.cli.main --plain
python -m app.cli.main -p "your task"
```

## Installation

### One-click Script (Linux / macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/YickelFuboo/Moma-Coder/main/scripts/install.sh | bash
```

The script automatically: detects Python → creates an isolated virtual environment → downloads the wheel from GitHub Releases → runs `moma-setup` → configures PATH.

Install a specific version:

```bash
MOMA_VERSION=v0.1.0 curl -fsSL https://raw.githubusercontent.com/YickelFuboo/Moma-Coder/main/scripts/install.sh | bash
```

### One-click Script (Windows)

Run in PowerShell:

```powershell
iwr -useb https://raw.githubusercontent.com/YickelFuboo/Moma-Coder/main/scripts/install.ps1 | iex
```

The script automatically: detects Python → creates an isolated virtual environment → downloads the wheel from GitHub Releases → runs `moma-setup` → adds to user PATH. After installation, **restart your terminal** for PATH changes to take effect.

Install a specific version:

```powershell
$env:MOMA_VERSION="v0.1.0"; iwr -useb https://raw.githubusercontent.com/YickelFuboo/Moma-Coder/main/scripts/install.ps1 | iex
```

### Manual Installation from GitHub Releases

1. Go to the [Releases page](https://github.com/YickelFuboo/Moma-Coder/releases) and download the latest `.whl` file
2. Install into a virtual environment and initialize:

```bash
python -m venv ~/.moma/venv
~/.moma/venv/bin/pip install momacoder-*.whl
~/.moma/venv/bin/moma-setup
```

3. Add `~/.moma/venv/bin` to PATH, or create a symlink to `~/.local/bin`

### Installation from Source (Deployment)

Code is copied to site-packages, independent from the repo — repo changes won't affect the installed stable version:

```powershell
cd F:\MOMA\Moma-Coder
.\.venv\Scripts\Activate.ps1
python -m pip install .   # Without -e: installs a baseline copy to site-packages
moma-setup
```

Note: Do not use `--user` when a venv is activated, as it will cause "User site-packages are not visible" errors.

`moma-setup` (or `python -m app.install`) syncs `agents` / `skills` / `models` to `~/.moma`; if `~/.moma/env` doesn't exist yet, it copies from `env.example`.

### Starting the Service

After installation, run directly — all configuration is read from `~/.moma/env`, no extra environment variables needed:

```powershell
moma
```

| Scenario | Startup Command | Config Source |
|----------|----------------|---------------|
| Development | `python -m app.cli.main` | `~/.moma/env` |
| Deployment | `moma` | `~/.moma/env` |

### Commands

MOMA automatically scans the workspace in the background on startup to trigger CodeBase pre-analysis (indexing code symbols, building call graphs, preparing semantic retrieval). Use the following commands to check progress or force a rescan.

**CLI Subcommands**

```powershell
moma codebase            # View current status (alias for status)
moma codebase status     # Same as above
moma codebase rescan     # Force a rescan
```

`moma codebase` only starts DB/scheduler without the Agent/TUI flow — useful for scripting status checks or scheduled rescans.

**TUI Slash Commands**

In the `moma` interactive interface:

```
/codebase            # Outputs a multi-line status report to the chat area
/codebase rescan     # Triggers a rescan and immediately displays the new status
```

**Real-time Progress Panel**

The TUI polls scan status every 2 seconds:

- Scanning in progress: the welcome-box right side automatically switches from "Recent activity" to a "CodeBase Progress" card showing info like `running (3s ago) · 217/350 emb`
- After welcome-box is hidden (after first message), a bottom status-row indicator persists: `· CB ⟳ 217/350 emb`
- Scan complete: panel switches back to "Recent activity", indicator disappears

### Updating

After code updates, reinstall:

```powershell
cd F:\MOMA\Moma-Coder
.\.venv\Scripts\Activate.ps1
python -m pip install .      # Overwrites the old version
moma-setup                   # Syncs potentially updated agents/skills/models
```

The workspace defaults to the current directory.

If you get a "command not found" error, confirm the venv is activated or add `.venv\Scripts` to PATH.

### Uninstalling

```powershell
pip uninstall MomaCoder
```
