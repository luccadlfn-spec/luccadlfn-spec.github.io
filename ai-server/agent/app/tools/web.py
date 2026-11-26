"""web_search (SearXNG) and fetch_url (guarded HTTP GET plus text extraction)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import warnings
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

from ..history import clip_middle
from .base import MAX_RESULT_CHARS, ToolError
from .netguard import Blocked, Resolver, resolve_public, system_resolver

MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5
SEARCH_TIMEOUT = 20.0
MAX_SEARCH_BYTES = 1024 * 1024
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

_TEXT_TYPES = ("application/xhtml+xml", "application/xml", "application/json")
_REDIRECTS = (301, 302, 303, 307, 308)


def is_text_type(content_type: str) -> bool:
    mime = content_type.split(";", 1)[0].strip().lower()
    return mime.startswith("text/") or mime in _TEXT_TYPES or mime.endswith(("+xml", "+json"))


async def read_capped(resp: httpx.Response, limit: int) -> tuple[bytes, bool]:
    """Read at most `limit` (decoded) bytes; the bool says whether more was left unread."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return b"".join(chunks)[:limit], True
    return b"".join(chunks), False


# ---------------------------------------------------------------- fetch_url

@dataclass
class Page:
    url: str
    content_type: str
    body: bytes
    truncated: bool


class Fetcher:
    """HTTP(S) GET that only ever connects to addresses approved by the SSRF guard."""

    def __init__(self, resolver: Resolver = system_resolver,
                 transport: httpx.AsyncBaseTransport | None = None, verify: object = True):
        self._resolver = resolver
        # trust_env=False: a proxy from the environment would connect on our behalf
        # and make the address check meaningless. No keep-alive: every hop is a
        # fresh connection to the address that was just checked.
        self._http = httpx.AsyncClient(
            transport=transport, verify=verify, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(FETCH_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en;q=0.9,*;q=0.5",
                     "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _open(self, url: str) -> httpx.Response:
        """Check one hop and open it, connected to the checked address."""
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, ValueError, TypeError):
            raise Blocked("that is not a valid URL") from None
        if parsed.scheme not in ("http", "https"):
            raise Blocked("refused: only http and https URLs can be fetched")
        if parsed.userinfo:
            raise Blocked("refused: URLs with a user name or password are not allowed")
        host = parsed.raw_host.decode("ascii", "replace")
        if not host:
            raise Blocked("the URL has no host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        address = await resolve_public(host, port, self._resolver)
        extensions = {}
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            if parsed.scheme == "https":
                # The URL now carries the address; the certificate must still match the name.
                extensions["sni_hostname"] = host
        request = self._http.build_request(
            "GET", parsed.copy_with(host=address),
            headers={"Host": parsed.netloc.decode("ascii", "replace")}, extensions=extensions)
        return await self._http.send(request, stream=True)

    async def fetch(self, url: str) -> Page:
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                current = url.strip()
                for _hop in range(MAX_REDIRECTS + 1):
                    resp = await self._open(current)
                    try:
                        if resp.status_code in _REDIRECTS:
                            location = resp.headers.get("location")
                            if not location:
                                raise ToolError(f"HTTP {resp.status_code} redirect without a target")
                            try:
                                current = str(httpx.URL(current).join(location))
                            except (httpx.InvalidURL, ValueError, TypeError):
                                raise Blocked("the redirect target is not a valid URL") from None
                            continue
                        if resp.status_code >= 400:
                            raise ToolError(f"the server answered HTTP {resp.status_code}")
                        ctype = resp.headers.get("content-type", "")
                        if not is_text_type(ctype):
                            raise ToolError(f"not a text page (content type '{ctype[:60] or 'missing'}')")
                        body, truncated = await read_capped(resp, MAX_DOWNLOAD_BYTES)
                        return Page(current, ctype, body, truncated)
                    finally:
                        await resp.aclose()
                raise ToolError(f"more than {MAX_REDIRECTS} redirects")
        except Blocked as exc:
            raise ToolError(str(exc)) from None
        except TimeoutError:
            raise ToolError(f"the page did not load within {FETCH_TIMEOUT:.0f} s") from None
        except httpx.HTTPError as exc:
            raise ToolError(f"could not load the page ({type(exc).__name__})") from None


class _TextOnly(HTMLParser):
    """Fallback extractor: visible text without scripts and styles."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if not self._depth and data.strip():
            self.parts.append(data.strip())


def _charset(content_type: str) -> str | None:
    m = re.search(r"charset=([\w.:-]+)", content_type, re.I)
    return m.group(1) if m else None


def _decode(body: bytes, content_type: str) -> str:
    for enc in (_charset(content_type), "utf-8"):
        if enc:
            try:
                return body.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
    return body.decode("utf-8", "replace")


# Bounded on purpose: with an open-ended pattern, a page full of "<title" openers and no
# closer made one search take time quadratic in its size (about 27 s of CPU per fetch).
_TITLE_OPEN = re.compile(r"<title\b[^>]{0,300}>", re.I)


def page_title(text: str) -> str:
    """The text of the first <title> element, found in linear time."""
    m = _TITLE_OPEN.search(text, 0, 200_000)
    if not m:
        return ""
    rest = text[m.end():m.end() + 2000]
    end = rest.lower().find("</title")
    return re.sub(r"\s+", " ", rest[:end]).strip()[:200] if end >= 0 else ""


def extract_text(body: bytes, content_type: str, url: str) -> tuple[str, str]:
    """Return (title, readable text). Runs in a worker thread: parsing is CPU work."""
    mime = content_type.split(";", 1)[0].strip().lower()
    text = _decode(body, content_type)
    if "html" not in mime:
        return "", text
    title = page_title(text)
    extracted = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")     # optional-dependency notices from its imports
            import trafilatura                  # imported late: slow to load, only needed here
        extracted = trafilatura.extract(text, url=url, include_comments=False, include_links=False)
    except Exception:
        extracted = None
    if not extracted or len(extracted) < 200:
        parser = _TextOnly()
        try:
            parser.feed(text)
        except Exception:
            pass
        fallback = "\n".join(parser.parts)
        if len(fallback) > len(extracted or ""):
            extracted = fallback
    return title, extracted or ""


async def fetch_url(fetcher: Fetcher, url: str, max_chars: int) -> str:
    max_chars = max(200, min(MAX_RESULT_CHARS, max_chars))
    page = await fetcher.fetch(url)
    title, text = await asyncio.to_thread(extract_text, page.body, page.content_type, page.url)
    if not text.strip():
        raise ToolError("the page has no readable text (it may need JavaScript)")
    header = (f"{title}\n" if title else "") + page.url
    note = "\n[page was cut at the 2 MB download limit]" if page.truncated else ""
    return f"{header}\n\n{clip_middle(text.strip(), max_chars)}{note}"


# ---------------------------------------------------------------- web_search

class Searcher:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(transport=transport, trust_env=False, timeout=SEARCH_TIMEOUT,
                                       limits=httpx.Limits(max_connections=2))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search(self, query: str, max_results: int) -> str:
        query = query.strip()[:300]
        if not query:
            raise ToolError("the search query is empty")
        max_results = max(1, min(8, max_results))
        try:
            async with self._http.stream("GET", f"{self._base}/search",
                                         params={"q": query, "format": "json"}) as resp:
                if resp.status_code != 200:
                    raise ToolError(f"the search service answered HTTP {resp.status_code}")
                raw, truncated = await read_capped(resp, MAX_SEARCH_BYTES)
        except httpx.HTTPError as exc:
            raise ToolError(f"the search service is not reachable ({type(exc).__name__})") from None
        try:
            if truncated:
                raise ValueError("too large")
            data = json.loads(raw)
            results = data.get("results") or []
        except (ValueError, AttributeError):
            raise ToolError("the search service sent an unreadable answer") from None
        lines = []
        for item in results:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:150]
            snippet = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()[:300]
            lines.append(f"{len(lines) + 1}. {title or '(no title)'}\n   {str(item['url'])[:500]}"
                         + (f"\n   {snippet}" if snippet else ""))
            if len(lines) >= max_results:
                break
        if not lines:
            dead = data.get("unresponsive_engines") or []
            return "No results." + (f" ({len(dead)} search engines did not respond.)" if dead else "")
        return "\n".join(lines)
