import ipaddress
import socket
from typing import Optional, TypedDict
from urllib.parse import urlparse


_BLOCKED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

class FetchResult(TypedDict, total=False):
    url: str
    final_url: str
    status: int
    extractor: str
    truncated: bool
    length: int
    text: str
    error: Optional[str]

def validate_fetch_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"Only http/https allowed, got '{parsed.scheme or 'none'}'"
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return False, "Missing domain"
        if host in _BLOCKED_HOSTS:
            return False, f"Blocked host: {host}"
        if host.endswith(".local") or host.endswith(".internal"):
            return False, f"Blocked host suffix: {host}"
        try:
            addr_infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return True, ""
        for info in addr_infos:
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False, f"Blocked private/reserved address: {ip_str}"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)
    return True, ""
