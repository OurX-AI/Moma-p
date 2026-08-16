#!/usr/bin/env bash
#
# MomaCoder 一键安装脚本
#
# 用法:
#   curl -fsSL https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.sh | bash
#
# 指定版本安装:
#   MOMA_VERSION=v0.1.0 curl -fsSL https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.sh | bash
#
set -euo pipefail

# ============ 配置 ============
GITHUB_USER="OurX-AI"
GITHUB_REPO="Moma-p"
INSTALL_DIR="${MOMA_HOME:-$HOME/.moma}"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="$HOME/.local/bin"
MOMA_VERSION="${MOMA_VERSION:-latest}"

# ============ 颜色输出 ============
if [ -t 1 ]; then
    C_RED='\033[0;31m'
    C_GREEN='\033[0;32m'
    C_YELLOW='\033[1;33m'
    C_NC='\033[0m'
else
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_NC=''
fi

info()  { printf "${C_GREEN}[INFO]${C_NC} %s\n" "$*"; }
warn()  { printf "${C_YELLOW}[WARN]${C_NC} %s\n" "$*"; }
error() { printf "${C_RED}[ERROR]${C_NC} %s\n" "$*" >&2; }

# ============ 检测操作系统 ============
detect_os() {
    case "$(uname -s)" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "macos" ;;
        MINGW*|MSYS*|CYGWIN*)
            error "Windows 请使用 WSL，或手动安装: pip install <wheel文件>"
            exit 1
            ;;
        *)
            error "不支持的操作系统: $(uname -s)"
            exit 1
            ;;
    esac
}

# ============ 检测 Python ============
check_python() {
    local cmd version major minor
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            version="$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")"
            major="${version%%.*}"
            minor="${version#*.}"
            if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ] && [ "$minor" -lt 13 ]; then
                info "检测到 Python $version ($cmd)"
                echo "$cmd"
                return 0
            fi
        fi
    done
    error "需要 Python 3.10 ~ 3.12，但未找到符合条件的 Python。"
    error "请从 https://www.python.org/downloads/ 安装后重试。"
    exit 1
}

# ============ 获取 wheel 下载地址 ============
get_wheel_url() {
    local api_url
    if [ "$MOMA_VERSION" = "latest" ]; then
        api_url="https://api.github.com/repos/${GITHUB_USER}/${GITHUB_REPO}/releases/latest"
    else
        api_url="https://api.github.com/repos/${GITHUB_USER}/${GITHUB_REPO}/releases/tags/${MOMA_VERSION}"
    fi

    info "查询 GitHub Release: $MOMA_VERSION"

    "$PYTHON_CMD" -c "
import urllib.request, json, sys
url = '${api_url}'
req = urllib.request.Request(url, headers={'User-Agent': 'moma-installer'})
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
except Exception as e:
    sys.stderr.write(f'GitHub API 请求失败: {e}\n')
    sys.exit(1)
for asset in data.get('assets', []):
    if asset['name'].endswith('.whl'):
        print(asset['browser_download_url'])
        break
else:
    sys.stderr.write('Release 中未找到 .whl 文件\n')
    sys.exit(1)
" || {
        error "获取 wheel 下载地址失败，请检查网络或版本号是否正确。"
        error "也可手动下载: https://github.com/${GITHUB_USER}/${GITHUB_REPO}/releases"
        exit 1
    }
}

# ============ 创建符号链接 ============
create_symlinks() {
    mkdir -p "$BIN_DIR"

    local cmd target link
    for cmd in moma moma-setup; do
        target="$VENV_DIR/bin/$cmd"
        link="$BIN_DIR/$cmd"

        # 移除旧链接
        if [ -L "$link" ] || [ -f "$link" ]; then
            rm -f "$link"
        fi

        if [ -f "$target" ]; then
            ln -s "$target" "$link"
            info "已创建命令: $cmd -> $target"
        else
            warn "未找到 $target，跳过"
        fi
    done
}

# ============ 配置 PATH ============
ensure_path() {
    case ":${PATH}:" in
        *":${BIN_DIR}:"*)
            info "$BIN_DIR 已在 PATH 中"
            return 0
            ;;
    esac

    local profile
    case "${SHELL:-bash}" in
        */zsh)  profile="$HOME/.zshrc" ;;
        */bash) profile="$HOME/.bashrc" ;;
        *)      profile="$HOME/.profile" ;;
    esac

    info "将 $BIN_DIR 添加到 PATH ($profile)"

    {
        echo ""
        echo "# MomaCoder"
        echo 'export PATH="'"$BIN_DIR"':$PATH"'
    } >> "$profile"

    warn "PATH 已写入 $profile，请运行以下命令使其生效（或重新打开终端）:"
    warn "  source $profile"
}

# ============ 主流程 ============
main() {
    echo ""
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║          MomaCoder 安装程序               ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo ""

    local os
    os="$(detect_os)"
    info "操作系统: $os"

    PYTHON_CMD="$(check_python)"

    # 创建安装目录
    mkdir -p "$INSTALL_DIR"

    # 创建虚拟环境
    if [ ! -d "$VENV_DIR" ]; then
        info "创建虚拟环境: $VENV_DIR"
        "$PYTHON_CMD" -m venv "$VENV_DIR"
    else
        info "虚拟环境已存在: $VENV_DIR"
    fi

    local venv_pip="$VENV_DIR/bin/pip"
    local venv_python="$VENV_DIR/bin/python"

    # 升级 pip
    info "升级 pip..."
    "$venv_python" -m pip install --upgrade pip --quiet

    # 获取 wheel 地址并安装
    local wheel_url
    wheel_url="$(get_wheel_url)"
    info "下载并安装: $wheel_url"
    "$venv_pip" install "$wheel_url"

    # 运行 moma-setup 初始化运行时数据
    info "初始化运行时数据..."
    "$VENV_DIR/bin/moma-setup" || warn "moma-setup 执行出错，可稍后手动运行: $VENV_DIR/bin/moma-setup"

    # 创建符号链接到 ~/.local/bin
    create_symlinks

    # 配置 PATH
    ensure_path

    echo ""
    info "✓ 安装完成！"
    echo ""
    echo "  使用方法:"
    echo "    moma              # 启动 MomaCoder"
    echo "    moma-setup        # 重新初始化运行时数据"
    echo ""
    echo "  配置文件: $INSTALL_DIR/env"
    echo "  虚拟环境: $VENV_DIR"
    echo ""
    echo "  指定版本安装:"
    echo "    MOMA_VERSION=v0.1.0 curl -fsSL https://raw.githubusercontent.com/${GITHUB_USER}/${GITHUB_REPO}/main/scripts/install.sh | bash"
    echo ""
}

main "$@"
