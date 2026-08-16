#!/usr/bin/env bash
# WeWrite 依赖安装脚本
#
# 在 {skill_dir}/.venv 里创建虚拟环境并安装 requirements。
# 解决 macOS Homebrew Python 3.11+ 的 PEP 668
# (externally-managed-environment) 限制 —— 该限制会让直接
# `pip install -r requirements.txt` 报错。
#
# 用法：  bash install.sh
# 幂等：  可重复运行，已存在的 venv 不会重建。
#
# 安装后无需手动 activate —— SKILL.md 会自动优先使用
# .venv/bin/python3（见 SKILL.md 的「Python 解释器约定」）。

set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "✗ 找不到 $PYTHON。请先安装 Python 3.11+（macOS: brew install python）。" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "→ 创建虚拟环境 .venv ..."
  "$PYTHON" -m venv .venv
else
  echo "→ 复用已有的 .venv"
fi

echo "→ 安装依赖到 .venv ..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt

echo ""
echo "✓ 完成。依赖已装入 $(pwd)/.venv"
echo "  无需手动 activate —— skill 运行时会自动使用 .venv/bin/python3。"
