"""将仓库种子数据同步到默认运行时目录 ~/.moma。"""
from __future__ import annotations
import os
import shutil
from pathlib import Path


class RuntimeDataBootstrap:
    """安装时把 data 下的模板复制到 ~/.moma。"""
    SEED_DIR_NAMES = ("agents", "skills", "models")

    @staticmethod
    def repo_root() -> Path:
        return Path(__file__).resolve().parents[1]

    @classmethod
    def seed_root(cls) -> Path:
        return cls.repo_root() / "data"

    @classmethod
    def target_root(cls) -> Path:
        return (Path.home() / ".moma").expanduser().resolve()

    @classmethod
    def ensure_seeded(cls) -> Path:
        """把仓库 data 种子同步到 ~/.moma（已有文件不覆盖）。"""
        target = cls.target_root()
        target.mkdir(parents=True, exist_ok=True)
        source = cls.seed_root()
        if source.is_dir():
            for name in cls.SEED_DIR_NAMES:
                cls._merge_copy_tree(source / name, target / name)
        cls._ensure_env_file(target)
        return target

    @classmethod
    def _merge_copy_tree(cls, src: Path, dst: Path) -> int:
        if not src.is_dir():
            return 0
        copied = 0
        dst.mkdir(parents=True, exist_ok=True)
        for root, _dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            out_dir = dst / rel
            out_dir.mkdir(parents=True, exist_ok=True)
            for name in files:
                if name.endswith(".pyc") or name == ".DS_Store":
                    continue
                src_file = Path(root) / name
                # .example 后缀的文件去掉后缀再写入目标
                target_name = name[:-8] if name.endswith(".example") else name
                dst_file = out_dir / target_name
                if dst_file.exists():
                    continue
                shutil.copy2(src_file, dst_file)
                copied += 1
        return copied

    @classmethod
    def _ensure_env_file(cls, target: Path) -> None:
        """仅在缺失时从 env.example 生成；已有 env 不改动。"""
        dest = target / "env"
        if dest.is_file():
            return
        example = cls.repo_root() / "env.example"
        if example.is_file():
            shutil.copy2(example, dest)
