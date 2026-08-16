import re

_IDENTIFIER_RE = re.compile(
    r"^(builtin|bundled|github|clawhub|skills-sh|skills\.sh|well-known|wellknown|lobehub):(.+)$",
    re.IGNORECASE,
)
_WELLKNOWN_INDEX_RE = re.compile(
    r"^https?://[^/]+/\.well-known/(?:agent-skills|skills)/(?:index\.json)?$",
    re.IGNORECASE,
)
_CLAWHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?clawhub\.ai/(?:skills/)?(?P<slug>[a-zA-Z0-9][a-zA-Z0-9_-]*)/?(?:\?.*)?$",
    re.IGNORECASE,
)
_GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/?#]+)(?:/(?P<kind>tree|blob)/(?P<branch>[^/]+)/(?P<subpath>[^?#]*))?",
    re.IGNORECASE,
)
_LOBEHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:lobehub\.com|market\.lobehub\.com)/(?:skills/|s/skills/)(?P<slug>[^/?#]+)/?(?:\?.*)?$",
    re.IGNORECASE,
)


def _github_path_from_url(url: str) -> str | None:
    match = _GITHUB_URL_RE.match(url.strip())
    if not match:
        return None
    owner = match.group("owner")
    repo = match.group("repo")
    subpath = (match.group("subpath") or "").strip("/")
    if not subpath:
        return f"{owner}/{repo}"
    if subpath.endswith("/SKILL.md"):
        subpath = subpath[: -len("/SKILL.md")]
    elif subpath.endswith("SKILL.md"):
        subpath = subpath[: -len("SKILL.md")].strip("/")
    return f"{owner}/{repo}/{subpath}" if subpath else f"{owner}/{repo}"


def _clawhub_slug_from_url(url: str) -> str | None:
    match = _CLAWHUB_URL_RE.match(url.strip())
    if not match:
        return None
    return match.group("slug")


def _lobehub_slug_from_url(url: str) -> str | None:
    match = _LOBEHUB_URL_RE.match(url.strip())
    if not match:
        return None
    return match.group("slug")


def normalize_identifier(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError("identifier is required")
    match = _IDENTIFIER_RE.match(text)
    if match:
        source = match.group(1).lower()
        path = match.group(2).strip()
        if source == "bundled":
            source = "builtin"
        if source == "skills.sh":
            source = "skills-sh"
        if source == "wellknown":
            source = "well-known"
        return f"{source}:{path}"
    lower = text.lower()
    if text.startswith("http"):
        if "skills.sh/" in lower:
            path = text.split("skills.sh/", 1)[1].strip("/").split("?")[0].split("#")[0]
            return f"skills-sh:{path}"
        clawhub_slug = _clawhub_slug_from_url(text)
        if clawhub_slug:
            return f"clawhub:{clawhub_slug}"
        lobehub_slug = _lobehub_slug_from_url(text)
        if lobehub_slug:
            return f"lobehub:{lobehub_slug}"
        github_path = _github_path_from_url(text)
        if github_path:
            return f"github:{github_path}"
        if "/.well-known/agent-skills/" in lower or "/.well-known/skills/" in lower:
            from urllib.parse import urlparse
            from .sources.well_known import parse_wellknown_identifier, wrap_identifier
            origin, skill_name = parse_wellknown_identifier(text)
            return wrap_identifier(origin, skill_name)
        if _WELLKNOWN_INDEX_RE.match(text.split("?")[0].split("#")[0]):
            from urllib.parse import urlparse
            parsed = urlparse(text)
            host = parsed.netloc
            return f"well-known:{host}"
    if "/" in text:
        return f"github:{text}"
    return f"builtin:{text}"


def parse_identifier(raw: str) -> tuple[str, str]:
    text = normalize_identifier(raw)
    match = _IDENTIFIER_RE.match(text)
    if not match:
        raise ValueError(f"invalid identifier: {raw}")
    source = match.group(1).lower()
    if source == "bundled":
        source = "builtin"
    if source == "skills.sh":
        source = "skills-sh"
    if source == "wellknown":
        source = "well-known"
    return source, match.group(2).strip()


def github_repo_from_identifier(path: str) -> str | None:
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"
