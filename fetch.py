"""Network fetching for the crawler. Every request goes through netguard.safe_urlopen (SSRF protection)."""
import http.client
import re
import urllib.error
import urllib.request

from ..core.config import settings
from ..core.netguard import safe_urlopen

MAX_BYTES = 2_000_000
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


def fetch_url(url: str, timeout: float = 10.0) -> tuple[str, str]:
    """Return (final_url, html). Raises on non-HTML, errors, oversize or SSRF-blocked targets (BlockedURL)."""
    req = urllib.request.Request(url, headers={"User-Agent": settings.bot_name, "Accept": "text/html"})
    with safe_urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype.lower():
            raise ValueError(f"not html: {ctype}")
        raw = resp.read(MAX_BYTES)
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        charset = m.group(1) if m else "utf-8"
        try:
            html = raw.decode(charset, errors="replace")
        except LookupError:
            html = raw.decode("utf-8", errors="replace")
        return resp.geturl(), html


def fetch_raw(url: str, timeout: float = 10.0, max_bytes: int = 5_000_000, accept: str = "*/*") -> bytes:
    """Fetch any content type (robots.txt, sitemaps). HTTP errors raise urllib.error.HTTPError."""
    req = urllib.request.Request(url, headers={"User-Agent": settings.bot_name, "Accept": accept})
    with safe_urlopen(req, timeout=timeout) as resp:
        return resp.read(max_bytes)


def is_transient(exc: BaseException) -> bool:
    """True for failures worth retrying later (timeouts, resets, 429/5xx). Everything else is permanent."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in TRANSIENT_HTTP
    if isinstance(exc, urllib.error.URLError):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError, http.client.HTTPException))


def retry_after_seconds(exc: BaseException) -> float | None:
    """Seconds from a numeric Retry-After header (HTTP-date form is ignored)."""
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None
