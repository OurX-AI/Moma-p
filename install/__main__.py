"""部署安装入口：python -m install"""
from install.runtime_bootstrap import RuntimeDataBootstrap


def main() -> None:
    target = RuntimeDataBootstrap.ensure_seeded()
    print(f"MOMA 运行时目录已就绪: {target}")
    print(f"  agents/skills/models 已同步（已有文件不覆盖）")
    print(f"  配置: {target / 'env'}（缺失时从 env.example 生成）")
    print()
    print(f"启动 MOMA 服务：")
    print(f"")
    print(f"  Linux/macOS:")
    print(f"    moma")
    print(f"  Windows (PowerShell/cmd):")
    print(f"    moma")
    print(f"")
    print(f"  配置统一读取 ~/.moma/env，无需额外环境变量。")


if __name__ == "__main__":
    main()
