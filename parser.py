"""HTML parsing and URL normalisation for the crawler."""
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
BAD_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|ico|pdf|zip|gz|rar|mp3|mp4|avi|mov|css|js|json|xml|woff2?|ttf|exe|apk|dmg)$", re.I
)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_desc = ""
        self.html_lang: str | None = None
        self.canonical: str | None = None  # <link rel="canonical">
        self.base_href: str | None = None  # <base href>
        self.robots: set[str] = set()  # <meta name="robots"> directives (lower-case)
        self.links: list[str] = []
        self._text: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html" and a.get("lang"):
            self.html_lang = a["lang"]
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            rel = (a.get("rel") or "").lower()
            if "nofollow" not in rel:
                self.links.append(a["href"])
        elif tag == "link":
            if "canonical" in (a.get("rel") or "").lower().split() and a.get("href") and self.canonical is None:
                self.canonical = a["href"].strip()
        elif tag == "base":
            if a.get("href") and self.base_href is None:
                self.base_href = a["href"].strip()
        elif tag == "meta":
            name = (a.get("name") or "").lower()
            if name == "description":
                self.meta_desc = a.get("content") or ""
            elif name in ("robots", "mavebot"):
                self.robots.update(t.strip() for t in (a.get("content") or "").lower().split(",") if t.strip())

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._text.append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._text)).strip()

    @property
    def noindex(self) -> bool:
        return "noindex" in self.robots or "none" in self.robots

    @property
    def nofollow(self) -> bool:
        return "nofollow" in self.robots or "none" in self.robots


# URLs are primary / unique keys; PostgreSQL refuses btree entries above ~2.7 KB, so longer ones are never crawled.
MAX_URL_BYTES = 2000


def normalize_url(url: str) -> str | None:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    netloc = p.netloc.lower()
    if (p.scheme == "http" and netloc.endswith(":80")) or (p.scheme == "https" and netloc.endswith(":443")):
        netloc = netloc.rsplit(":", 1)[0]
    if BAD_EXT.search(p.path):
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))])
    out = urlunsplit((p.scheme, netloc, p.path or "/", query, ""))
    return out if len(out.encode("utf-8", "ignore")) <= MAX_URL_BYTES else None


def site_key(url: str) -> str:
    """Host without port and a leading 'www.' - two URLs with the same key belong to the same site."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host
