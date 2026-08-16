from pathlib import Path
from ..constants import BUILTIN_SKILLS_DIR
from ..models import SkillBundle, SkillMeta
from ...paths import SkillEntry, scan_root
from ..utils import content_hash, skill_name_from_bundle
from .base import SkillSource


class BuiltinSkillSource(SkillSource):
    source_id = "builtin"

    def __init__(self) -> None:
        self._index: dict[str, SkillEntry] | None = None
        self._index_root_mtime: float | None = None

    def _scan_index(self) -> dict[str, SkillEntry]:
        if not BUILTIN_SKILLS_DIR.is_dir():
            self._index = {}
            self._index_root_mtime = None
            return {}
        try:
            stamp = BUILTIN_SKILLS_DIR.stat().st_mtime
        except OSError:
            self._index = {}
            self._index_root_mtime = None
            return {}
        if self._index is not None and self._index_root_mtime == stamp:
            return self._index
        self._index = scan_root(BUILTIN_SKILLS_DIR)
        self._index_root_mtime = stamp
        return self._index

    def _entry_for_dir(self, skill_dir: Path) -> SkillEntry | None:
        try:
            rel = str(skill_dir.resolve().relative_to(BUILTIN_SKILLS_DIR.resolve())).replace("\\", "/")
        except ValueError:
            return None
        for entry in self._scan_index().values():
            if entry.rel_path == rel:
                return entry
        return None

    def _meta_from_entry(self, entry: SkillEntry) -> SkillMeta:
        return SkillMeta(
            name=entry.name,
            description=entry.description,
            source=self.source_id,
            identifier=f"builtin:{entry.rel_path}",
            trust_level="builtin",
            path=str(entry.abs_path),
            extra={"category": entry.category},
        )

    def _read_dir_files(self, skill_dir: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        for path in skill_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = str(path.relative_to(skill_dir)).replace("\\", "/")
            if ".." in rel.split("/"):
                continue
            try:
                files[rel] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return files

    def meta_from_skill_dir(self, skill_dir: Path) -> SkillMeta | None:
        entry = self._entry_for_dir(skill_dir)
        if entry:
            return self._meta_from_entry(entry)
        if not (skill_dir / "SKILL.md").is_file():
            return None
        return None

    def search(self, query: str, *, limit: int = 50, offset: int = 0) -> list[SkillMeta]:
        q = (query or "").strip().lower()
        items: list[SkillMeta] = []
        for entry in self._scan_index().values():
            meta = self._meta_from_entry(entry)
            if q and q not in meta.name.lower() and q not in meta.description.lower():
                continue
            items.append(meta)
        start = max(0, offset)
        end = start + max(1, limit)
        return items[start:end]

    def inspect(self, identifier_path: str) -> SkillMeta | None:
        name = identifier_path.strip()
        entry = self._scan_index().get(name)
        abs_path = entry.abs_path if entry else None
        if abs_path is None:
            rel = name.replace("\\", "/")
            candidate = (BUILTIN_SKILLS_DIR / rel).resolve()
            if (candidate / "SKILL.md").is_file():
                abs_path = candidate
        if abs_path is None:
            return None
        return self.meta_from_skill_dir(abs_path)

    def fetch_bundle(self, identifier_path: str) -> SkillBundle | None:
        key = identifier_path.strip()
        entry = self._scan_index().get(key)
        abs_path = entry.abs_path if entry else None
        if abs_path is None:
            rel = key.replace("\\", "/")
            candidate = (BUILTIN_SKILLS_DIR / rel).resolve()
            if (candidate / "SKILL.md").is_file():
                abs_path = candidate
        if abs_path is None or not (abs_path / "SKILL.md").is_file():
            return None
        files = self._read_dir_files(abs_path)
        skill_name = skill_name_from_bundle(files, abs_path.name)
        rel = str(abs_path.relative_to(BUILTIN_SKILLS_DIR)).replace("\\", "/")
        return SkillBundle(
            name=skill_name,
            files=files,
            source=self.source_id,
            identifier=f"builtin:{rel}",
            trust_level="builtin",
            metadata={"content_hash": content_hash(files)},
        )
