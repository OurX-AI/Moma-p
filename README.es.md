# MomaCoder

> AI-powered coding Agent that turns your terminal into an intelligent development partner

[![Python Version](https://img.shields.io/badge/Python-3.10+-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/OurX-AI/Moma-p-p.svg)](https://github.com/OurX-AI/Moma-p-p/releases)

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

**Linux / macOS**

```bash
curl -fsSL https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.sh | bash
```

**Windows (PowerShell)**

```powershell
iwr -useb https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.ps1 | iex
```

> Restart your terminal after installation for PATH changes to take effect.

### Configure Models

Two files need to be configured:

**1. Chat Model** — Edit `~/.moma/models/chat_models.json`:

```json
{
  "default": {
    "provider": "deepseek",
    "model": "deepseek-chat"
  },
  "models": {
    "deepseek": {
      "base_url": "https://api.deepseek.com/v1",
      "api_key": "sk-xxxx",
      "api_type": "openai",
      "instances": {
        "deepseek-chat": { "description": "DeepSeek Chat" },
        "deepseek-coder": { "description": "DeepSeek Coder" }
      }
    }
  }
}
```

**2. Embedding Model** — Edit `~/.moma/models/embedding_models.json` (required for CodeBase semantic search):

```json
{
  "default": {
    "provider": "siliconflow",
    "model": "BAAI/bge-m3"
  },
  "models": {
    "siliconflow": {
      "base_url": "https://api.siliconflow.cn/v1",
      "api_key": "sk-xxxx",
      "instances": {
        "BAAI/bge-m3": { "description": "BAAI BGE M3 Multilingual" }
      }
    }
  }
}
```

Supported providers: OpenAI, Anthropic, Google, DeepSeek, Alibaba Cloud, SiliconFlow, Ollama, and more.

### Start

```bash
moma
```

The first launch automatically scans your workspace to build a code index.

## ⚙️ Configuration

### Model Configuration

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

### Web Search Configuration (Optional)

Edit `~/.moma/env` to add search API keys:

```env
# Tavily Search
TAVILY_API_KEY=tvly-xxxx

# Or Serper (Google SERP, 2500 free queries)
SERPER_API_KEY=xxxx

# Or use free DuckDuckGo (no config needed)
WEB_SEARCH_PRIMARY=duckduckgo
```

## 📦 Installation Options

### One-click Scripts (Recommended)

| Platform | Command |
|----------|---------|
| Linux / macOS | `curl -fsSL https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.sh \| bash` |
| Windows | `iwr -useb https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.ps1 \| iex` |

Install a specific version:

```bash
MOMA_VERSION=v0.1.0 curl -fsSL https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.sh | bash
```

### Manual Installation

1. Download the `.whl` file from [Releases](https://github.com/OurX-AI/Moma-p/releases)
2. Install and initialize:

```bash
python -m venv ~/.moma/venv
~/.moma/venv/bin/pip install momacoder-*.whl
~/.moma/venv/bin/moma-setup
```

3. Add `~/.moma/venv/bin` to PATH

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

```bash
pip install --upgrade MomaCoder   # Update
pip uninstall MomaCoder           # Uninstall
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
