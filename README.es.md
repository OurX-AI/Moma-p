# MomaCoder

> AI-powered coding Agent that turns your terminal into an intelligent development partner

[![Python Version](https://img.shields.io/badge/Python-3.10+-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/OurX-AI/Moma-p.svg)](https://github.com/OurX-AI/Moma-p/releases)

MomaCoder is an AI coding agent designed for software development workflows. It provides a terminal-based user interface (TUI) that enables developers to collaborate with AI on code writing, debugging, refactoring, and documentation generation using natural language.

## ✨ Features

- **Codebase Awareness** — Automatically scans workspaces, builds symbol indexes and call graphs for semantic code retrieval
- **Multi-Agent Collaboration** — Sub-agent scheduling and team collaboration modes for parallel task processing
- **Tool Invocation** — Built-in code analysis, file operations, web search, shell execution, and MCP protocol support
- **Session Management** — Complete conversation history, context compression, and session persistence
- **Skill System** — Pluggable skill modules with custom workflow extensibility
- **Multi-Model Support** — OpenAI, Anthropic, Google, DeepSeek, Alibaba Cloud, Ollama, and 10+ providers

## 📸 Demo

<!-- TODO: Add terminal screenshot or GIF -->

## 🚀 Quick Start

### Install

#### One-click Scripts (Recommended)

Linux / macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.sh | bash
```

Windows:

```powershell
$script = "$env:TEMP\moma_install.ps1"; iwr -useb https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.ps1 -OutFile $script; & $script; Remove-Item $script -Force
```

Install a specific version:

Linux / macOS:

```bash
MOMA_VERSION=v0.1.0 curl -fsSL https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.sh | bash
```

Windows:

```powershell
$env:MOMA_VERSION="v0.1.0"; iwr -useb https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.ps1 | iex
```

> Restart your terminal after installation for PATH changes to take effect.

#### Windows Executable

Download `momacoder.exe` from [Releases](https://github.com/OurX-AI/Moma-p/releases), place it in any directory, and run directly:

```powershell
.\momacoder.exe
```

> No Python installation required — works out of the box.

#### Manual Installation

1. Download the `.whl` file from [Releases](https://github.com/OurX-AI/Moma-p/releases)
2. Install and initialize (run in any directory, replace `.whl` path with your actual download path):

```bash
python -m venv ~/.moma/venv
~/.moma/venv/bin/pip install ~/Downloads/momacoder-*.whl
~/.moma/venv/bin/moma-setup
```

3. Add the virtual environment executable directory to PATH:

   Linux / macOS:

   ```bash
   echo 'export PATH="$HOME/.moma/venv/bin:$PATH"' >> ~/.bashrc
   source ~/.bashrc
   ```

   Windows (PowerShell):

   ```powershell
   [Environment]::SetEnvironmentVariable("Path", "$env:USERPROFILE\.moma\venv\Scripts;" + [Environment]::GetEnvironmentVariable("Path", "User"), "User")
   ```

#### Install from Source

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

To use the `moma` command from any directory, add the virtual environment executable directory to PATH:

| Platform | Path |
|----------|------|
| Linux / macOS | `<Moma directory>/.venv/bin` |
| Windows | `<Moma directory>\.venv\Scripts` |

Alternatively, you can skip PATH configuration and launch directly from the project directory via `.venv/bin/moma` (or `.venv\Scripts\moma`).

### ⚙️ Configuration

#### Model Configuration

`~/.moma/models/` contains the following config files:

| File | Purpose | Required |
|------|---------|----------|
| `chat_models.json` | Chat models | Yes |
| `embedding_models.json` | Embedding models (semantic search) | Yes |
| `rerank_models.json` | Rerank models | Optional |
| `tts_models.json` | Text-to-speech models | Optional |
| `stt_models.json` | Speech-to-text models | Optional |
| `cv_models.json` | Computer vision models | Optional |

> After installation, `moma-setup` automatically copies example configs to `~/.moma/models/`

#### Environment Variables (Optional)

Edit `~/.moma/env` as needed. Common settings:

```env
# Web Search (enable one as needed)
TAVILY_API_KEY=tvly-xxxx     # Tavily
SERPER_API_KEY=xxxx           # Serper (Google SERP, 2500 free queries)
WEB_SEARCH_PRIMARY=duckduckgo # Free, no key needed
```

Additional switches and advanced settings (database, Redis, vector store, agent behavior, CodeBase, etc.) are documented in `env.example` — enable as needed.

### Start

```bash
moma
```

The first launch automatically scans your workspace to build a code index.

## 📖 Usage

### CLI Options

```bash
moma                              # Start interactive TUI
moma -p "write a quicksort"      # Execute a task directly
moma --plain                     # Plain text REPL mode
moma --resume <session_id>       # Resume a previous session
moma --model deepseek/deepseek-chat  # Specify model
moma -w /path/to/project         # Specify workspace directory
```

### CodeBase Management

```bash
moma codebase status              # View scan status
moma codebase rescan              # Force rescan
moma codebase experience          # View experience extraction status
```

TUI Slash Commands:

```
/codebase                         # Output status report
/codebase rescan                  # Trigger rescan
```

### Update & Uninstall

**One-click scripts / Manual install (whl):**

```bash
pip install --upgrade MomaCoder   # Update
pip uninstall MomaCoder           # Uninstall
```

**Install from source:**

```bash
# Update: pull and reinstall
cd Moma
git pull
.venv/bin/pip install -e .        # Linux / macOS
.venv\Scripts\pip install -e .    # Windows

# Uninstall
rm -rf ~/.moma                    # Remove runtime data
rm -rf .venv                      # Remove virtual environment (or on Windows: Remove-Item -Recurse -Force .venv)
# Remove .venv/bin (or .venv\Scripts) from PATH
```

## 🛠️ Development

All configuration is read from `~/.moma/env`. Before first development, run `moma-setup`:

```bash
moma-setup
python -m app.cli.main
```

Code changes take effect immediately.

## 🤝 Contributing

Contributions are welcome! Please follow these steps:

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Commit your changes: `git commit -m 'Add amazing feature'`
4. Push to the branch: `git push origin feature/amazing-feature`
5. Submit a Pull Request

## 🔧 Tech Stack

<details>
<summary>Click to expand</summary>

| Layer | Technology |
|-------|------------|
| Agent Framework | LangGraph + ReAct |
| User Interface | Textual (TUI) |
| Configuration | Pydantic Settings |
| Database | SQLite / PostgreSQL / MySQL |
| Vector Storage | Elasticsearch / OpenSearch / LanceDB |
| Graph Database | Neo4j |
| LLM Gateway | Multi-provider unified gateway |
| Code Analysis | Tree-sitter |
| Packaging | Poetry |

</details>

## 📄 License

[GNU General Public License v3.0](LICENSE)
