"""权限配置校验。"""
from __future__ import annotations
import logging
from .schemes import UnmatchedPolicy


class PermissionConfigValidator:
    """启动时校验 permissions 配置，非法项告警并跳过。"""

    VALID_UNMATCHED = {p.value for p in UnmatchedPolicy}

    @classmethod
    def validate_modes(cls, modes_raw: dict) -> dict:
        cleaned: dict = {}
        for name, body in modes_raw.items():
            key = str(name).strip()
            if not key or not isinstance(body, dict):
                logging.warning("permissions: skip invalid mode %r", name)
                continue
            unmatched = str(body.get("unmatched") or "allow").strip().lower()
            if unmatched not in cls.VALID_UNMATCHED:
                logging.warning(
                    "permissions: mode %r has invalid unmatched %r, fallback allow",
                    key,
                    unmatched,
                )
                unmatched = "allow"
            cleaned[key] = {
                "unmatched": unmatched,
                "description": str(body.get("description") or ""),
            }
        return cleaned

    @classmethod
    def validate_rule_list(cls, items: list, *, field: str) -> list[str]:
        out: list[str] = []
        for item in items or []:
            text = str(item).strip()
            if not text:
                continue
            if "(" in text and not text.endswith(")"):
                logging.warning("permissions.%s: skip malformed rule %r", field, text)
                continue
            out.append(text)
        return out
