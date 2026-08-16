import json
import re
from datetime import datetime, timezone
from typing import Any
from .constants import HUB_DIR, TAPS_FILE, TRUSTED_REPOS
from .lock import append_audit
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _ensure_hub_dir() -> None:
    HUB_DIR.mkdir(parents=True, exist_ok=True)


def normalize_github_repo(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError("GitHub tap must be owner/repo")
    if text.lower().startswith("github:"):
        text = text.split(":", 1)[1].strip()
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub tap must be owner/repo")
    repo = f"{parts[0]}/{parts[1]}"
    if not REPO_PATTERN.match(repo):
        raise ValueError(f"invalid GitHub repo format: {repo}")
    return repo


def load_custom_taps() -> list[str]:
    _ensure_hub_dir()
    if not TAPS_FILE.is_file():
        return []
    try:
        data = json.loads(TAPS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        raw = data.get("github_taps")
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in raw:
            try:
                repo = normalize_github_repo(str(item))
            except ValueError:
                continue
            if repo in seen or repo in TRUSTED_REPOS:
                continue
            seen.add(repo)
            result.append(repo)
        return result
    except (OSError, json.JSONDecodeError):
        return []


def save_custom_taps(repos: list[str]) -> None:
    _ensure_hub_dir()
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in repos:
        try:
            repo = normalize_github_repo(item)
        except ValueError:
            continue
        if repo in TRUSTED_REPOS or repo in seen:
            continue
        seen.add(repo)
        cleaned.append(repo)
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "github_taps": cleaned,
    }
    TAPS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_search_repos() -> frozenset[str]:
    return frozenset(set(TRUSTED_REPOS) | set(load_custom_taps()))


def list_github_taps() -> dict[str, Any]:
    custom = load_custom_taps()
    return {
        "builtin": sorted(TRUSTED_REPOS),
        "custom": custom,
        "search_repos": sorted(get_search_repos()),
    }


def add_github_tap(repo: str) -> dict[str, Any]:
    key = normalize_github_repo(repo)
    if key in TRUSTED_REPOS:
        raise ValueError(f"repo is already a built-in tap: {key}")
    taps = load_custom_taps()
    if key in taps:
        raise ValueError(f"tap already exists: {key}")
    taps.append(key)
    save_custom_taps(taps)
    append_audit(f"tap add repo={key}")
    return {"repo": key, "added": True}


def remove_github_tap(repo: str) -> dict[str, Any]:
    key = normalize_github_repo(repo)
    taps = load_custom_taps()
    if key not in taps:
        raise ValueError(f"custom tap not found: {key}")
    taps = [x for x in taps if x != key]
    save_custom_taps(taps)
    append_audit(f"tap remove repo={key}")
    return {"repo": key, "removed": True}
