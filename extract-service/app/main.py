from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
from typing import Dict, Any, Optional, Tuple
import html as html_lib
from html.parser import HTMLParser
import ipaddress
import json
import os
import re
import socket
from urllib.parse import urlparse
import requests

app = FastAPI(title="Extract Service")

API_KEY = (os.getenv("EXTRACT_API_KEY") or "").strip()
MAX_REDIRECTS = int(os.getenv("EXTRACT_MAX_REDIRECTS") or "5")
MAX_HTML_BYTES = int(os.getenv("EXTRACT_MAX_HTML_BYTES") or "5000000")

class ExtractReq(BaseModel):
    url: HttpUrl
    timeout: int = Field(default=15, ge=1, le=180)

@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, (dict, list)):
        detail = "Invalid url" if exc.status_code == 422 else "error"
    return JSONResponse(status_code=exc.status_code, content={"detail": str(detail)})


@app.exception_handler(RequestValidationError)
async def _request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": "Invalid url"})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return {"ok": True}


@app.get("/api/health")
def api_health():
    return {"ok": True}

def _check_api_key(request: Request) -> None:
    if not API_KEY:
        return
    got = (request.headers.get("x-api-key") or "").strip()
    if got != API_KEY:
        raise HTTPException(status_code=401, detail="unauthorized")


def _parse_timeout_s(raw: Any, default_s: int = 15) -> int:
    try:
        t = int(raw)
    except Exception:
        t = default_s
    t = max(1, min(180, t))
    return min(60, t)


def _extract_url_and_timeout_from_text(text: str) -> Tuple[Optional[str], Optional[int]]:
    s = (text or "").strip()
    if not s:
        return None, None

    m_url = re.search(r"\burl\s*=\s*[`\"']?(https?://[^`\"'\s]+)", s, flags=re.IGNORECASE)
    raw_url = m_url.group(1).strip() if m_url else None

    m_timeout = re.search(r"\btimeout\s*=\s*(\d+)", s, flags=re.IGNORECASE)
    timeout_s = int(m_timeout.group(1)) if m_timeout else None

    return raw_url, timeout_s


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(getattr(addr, "is_global", False))


def _validate_public_http_url(raw_url: str, timeout_s: int) -> str:
    s = (raw_url or "").strip().strip("`").strip("\"").strip("'")
    p = urlparse(s)

    if p.scheme not in {"http", "https"}:
        raise HTTPException(status_code=422, detail="Invalid url")
    if not p.netloc or p.username or p.password:
        raise HTTPException(status_code=422, detail="Invalid url")

    hostname = (p.hostname or "").strip().lower()
    if not hostname:
        raise HTTPException(status_code=422, detail="Invalid url")
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise HTTPException(status_code=422, detail="Invalid url")

    if _is_public_ip(hostname):
        return s
    try:
        ipaddress.ip_address(hostname)
        raise HTTPException(status_code=422, detail="Invalid url")
    except ValueError:
        pass

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(min(3, timeout_s))
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        for info in infos:
            ip = info[4][0]
            if not _is_public_ip(ip):
                raise HTTPException(status_code=422, detail="Invalid url")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid url")
    finally:
        socket.setdefaulttimeout(old_timeout)

    return s


def _short_fetch_reason(e: Exception) -> str:
    msg = str(e) or e.__class__.__name__
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg[:200]


def _extract_input(request: Request, body_json: Any, body_text: str) -> Tuple[str, int]:
    q_url = (request.query_params.get("url") or "").strip()
    if q_url:
        timeout_s = _parse_timeout_s(request.query_params.get("timeout"), 15)
        return _validate_public_http_url(q_url, timeout_s), timeout_s

    raw_url = None
    timeout_s = 15

    if isinstance(body_json, dict):
        raw_url = body_json.get("url")
        timeout_s = _parse_timeout_s(body_json.get("timeout"), 15)

    if not raw_url:
        t_url, t_timeout = _extract_url_and_timeout_from_text(body_text)
        raw_url = t_url or raw_url
        if t_timeout is not None:
            timeout_s = _parse_timeout_s(t_timeout, timeout_s)

    if not raw_url:
        raise HTTPException(status_code=422, detail="Invalid url")

    url = _validate_public_http_url(str(raw_url), timeout_s)
    return url, timeout_s


async def _fetch_html_rendered(url: str, timeout_s: int) -> str:
    timeout_ms = int(min(60, timeout_s) * 1000)
    try:
        from playwright.async_api import async_playwright
        from playwright.async_api import TimeoutError as PWTimeoutError
    except Exception as e:
        raise RuntimeError("playwright unavailable") from e

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                html = (await page.content() or "").strip()
                if not html:
                    raise HTTPException(status_code=502, detail="fetch failed: empty html")
                return html
            finally:
                await browser.close()
    except PWTimeoutError:
        raise HTTPException(status_code=504, detail="timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {_short_fetch_reason(e)}")


def _fetch_html(url: str, timeout_s: int) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ExtractService/1.0",
        "Accept": "text/html,application/xhtml+xml",
    }
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS

    connect_timeout_s = min(5, timeout_s)
    try:
        r = session.get(
            url,
            headers=headers,
            timeout=(connect_timeout_s, timeout_s),
            allow_redirects=True,
            stream=True,
        )
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="timeout")
    except requests.TooManyRedirects as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: too many redirects ({_short_fetch_reason(e)})")
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {_short_fetch_reason(e)}")

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"fetch failed: upstream http {r.status_code}")

    buf = bytearray()
    try:
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > MAX_HTML_BYTES:
                raise HTTPException(status_code=502, detail="fetch failed: html too large")
    finally:
        r.close()

    encoding = r.encoding or getattr(r, "apparent_encoding", None) or "utf-8"
    html = bytes(buf).decode(encoding, errors="replace").strip()
    if not html:
        raise HTTPException(status_code=502, detail="fetch failed: empty html")
    return html


async def _fetch_html_best_effort(url: str, timeout_s: int) -> str:
    try:
        return await _fetch_html_rendered(url, timeout_s)
    except HTTPException as e:
        if e.status_code == 504:
            raise
        return _fetch_html(url, timeout_s)
    except Exception:
        return _fetch_html(url, timeout_s)


def _extract_html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    t = html_lib.unescape(m.group(1))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:300]


class _HTMLToTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_pre = False

    def _append(self, s: str) -> None:
        if not s:
            return
        self.parts.append(s)

    def _ensure_paragraph_break(self) -> None:
        cur = "".join(self.parts)
        if not cur:
            return
        if cur.endswith("\n\n"):
            return
        if cur.endswith("\n"):
            self._append("\n")
            return
        self._append("\n\n")

    def _newline(self) -> None:
        cur = "".join(self.parts)
        if not cur or cur.endswith("\n"):
            self._append("\n")
            return
        self._append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = (tag or "").lower()
        if t in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if t in {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}:
            self._ensure_paragraph_break()
        elif t == "br":
            self._newline()
        elif t == "li":
            self._newline()
            self._append("- ")
        elif t == "pre":
            self._ensure_paragraph_break()
            self._in_pre = True

    def handle_endtag(self, tag: str) -> None:
        t = (tag or "").lower()
        if t in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return

        if t == "pre":
            self._in_pre = False
            self._ensure_paragraph_break()
        elif t in {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "blockquote", "li"}:
            self._ensure_paragraph_break()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if not data:
            return
        if self._in_pre:
            raw = data.replace("\r\n", "\n").replace("\r", "\n")
            for ln in raw.split("\n"):
                if ln == "" and raw.endswith("\n"):
                    self._append("\n")
                    continue
                if ln.strip():
                    self._append("    " + ln.rstrip())
                self._append("\n")
            return
        s = re.sub(r"\s+", " ", data).strip()
        if not s:
            return
        cur = "".join(self.parts)
        if not cur or cur.endswith(("\n", " ")):
            self._append(s)
        else:
            self._append(" " + s)


def _html_fragment_to_text(fragment_html: str) -> str:
    parser = _HTMLToTextParser()
    parser.feed(fragment_html or "")
    parser.close()
    out = "".join(parser.parts)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _dedupe_blocks(text: str) -> str:
    blocks = [b.strip() for b in (text or "").split("\n\n")]
    seen: set[str] = set()
    out: list[str] = []
    for b in blocks:
        if not b:
            continue
        key = re.sub(r"\s+", " ", b).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(b)
    return "\n\n".join(out).strip()


def _format_code_blocks(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_code = False
    for line in lines:
        is_code = bool(re.match(r"^(\t| {4,})\S", line))
        if is_code and not in_code:
            while out and out[-1] == "":
                out.pop()
            if out:
                out.append("")
            in_code = True
        if not is_code and in_code:
            while out and out[-1] == "":
                out.pop()
            out.append("")
            in_code = False

        if in_code:
            out.append(line.rstrip())
        else:
            s = re.sub(r"[ \t]+", " ", line.strip())
            out.append(s)

    if in_code:
        while out and out[-1] == "":
            out.pop()
        out.append("")

    merged = "\n".join(out)
    merged = re.sub(r"\n{3,}", "\n\n", merged)
    merged = re.sub(r"[ \t]+\n", "\n", merged)
    return merged.strip()


def _apply_limits(text: str) -> str:
    s = (text or "").strip()
    if len(s) > 12000:
        s = s[:12000].rstrip() + "\n\n（内容过长已截断）"
    return s


def _build_lead(author: str, date: str) -> str:
    parts: list[str] = []
    a = (author or "").strip()
    d = (date or "").strip()
    if a:
        parts.append(f"作者：{a}")
    if d:
        parts.append(f"发布时间：{d}")
    return " ".join(parts).strip()

@app.post("/extract")
@app.post("/api/extract")
async def extract(request: Request) -> Dict[str, Any]:
    _check_api_key(request)

    body_json: Any = None
    body_text = ""
    try:
        body_bytes = await request.body()
    except Exception:
        body_bytes = b""

    if body_bytes:
        body_text = body_bytes.decode("utf-8", errors="ignore")
        try:
            body_json = json.loads(body_text)
        except Exception:
            body_json = None

    url, timeout_s = _extract_input(request, body_json, body_text)
    html = await _fetch_html_best_effort(url, timeout_s)

    title = ""
    text = ""
    author = ""
    date = ""

    try:
        import trafilatura
        extracted = trafilatura.extract(html, include_comments=False, include_tables=False, output_format="txt")
        if extracted:
            text = extracted.strip()
        meta = trafilatura.extract_metadata(html)
        if meta and meta.title:
            title = meta.title.strip()
        if meta and getattr(meta, "author", None):
            author = (meta.author or "").strip()
        if meta and getattr(meta, "date", None):
            date = str(meta.date).strip()
    except Exception:
        pass

    if not text:
        try:
            from readability import Document
            doc = Document(html)
            title = title or (doc.short_title() or "").strip()
            text = _html_fragment_to_text(doc.summary())
        except Exception:
            pass

    if not title:
        title = _extract_html_title(html) or "原文快照"

    if not text:
        raise HTTPException(status_code=422, detail="extract failed")

    lead = _build_lead(author, date)
    formatted = _format_code_blocks(text)
    formatted = _dedupe_blocks(formatted)
    formatted = _apply_limits(formatted)

    if lead:
        formatted = f"{lead}\n\n{formatted}".strip()
        formatted = _apply_limits(formatted)

    if not formatted:
        raise HTTPException(status_code=422, detail="extract failed")

    return {"url": url, "title": title, "text": formatted}
