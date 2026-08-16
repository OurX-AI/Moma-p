"""沙箱配置解析。"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class SandboxConfig:
    enabled: bool = True
    allow_write: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    deny_network: bool = False

    @classmethod
    def from_dict(cls, raw: dict | None) -> SandboxConfig:
        if not isinstance(raw, dict):
            return cls()
        allow_write: list[str] = []
        deny_read: list[str] = []
        fs = raw.get("filesystem")
        if isinstance(fs, dict):
            for item in fs.get("allowWrite") or []:
                text = str(item).strip()
                if text:
                    allow_write.append(text)
            for item in fs.get("denyRead") or []:
                text = str(item).strip()
                if text:
                    deny_read.append(text)
        for item in raw.get("allowWrite") or []:
            text = str(item).strip()
            if text and text not in allow_write:
                allow_write.append(text)
        for item in raw.get("denyRead") or []:
            text = str(item).strip()
            if text and text not in deny_read:
                deny_read.append(text)
        return cls(
            enabled=bool(raw.get("enabled", True)),
            allow_write=allow_write,
            deny_read=deny_read,
            deny_network=bool(raw.get("denyNetwork", False)),
        )
