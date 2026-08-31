"""Polite HTTP session: retries, rate limiting, content-addressed downloads."""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import REQUEST_DELAY, REQUEST_TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

_last_call: dict[str, float] = {}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "vi,en;q=0.8"})
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = make_session()


def _throttle(url: str) -> None:
    host = url.split("/")[2] if "://" in url else url
    prev = _last_call.get(host)
    if prev is not None:
        wait = REQUEST_DELAY - (time.monotonic() - prev)
        if wait > 0:
            time.sleep(wait)
    _last_call[host] = time.monotonic()


def get(url: str, **kw) -> requests.Response:
    _throttle(url)
    kw.setdefault("timeout", REQUEST_TIMEOUT)
    r = SESSION.get(url, **kw)
    r.raise_for_status()
    return r


def get_json(url: str, **kw):
    return get(url, **kw).json()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, *, skip_existing: bool = True,
             offline: bool = False) -> dict:
    """Fetch ``url`` to ``dest``. Returns provenance metadata.

    Existing files are kept (the archive is append-only) unless the remote
    reports a different Content-Length. With ``offline`` an existing file is
    used as-is and no request is made at all — that is what lets a parser fix
    be replayed over the whole archive without re-downloading it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if offline:
        if not dest.exists():
            raise FileNotFoundError(f"{dest} not in the archive (offline mode)")
        return {"url": url, "path": str(dest), "bytes": dest.stat().st_size,
                "sha256": sha256_file(dest), "downloaded": False}
    if dest.exists() and skip_existing:
        try:
            _throttle(url)
            head = SESSION.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            remote_len = int(head.headers.get("Content-Length", -1))
        except Exception:
            remote_len = -1
        if remote_len in (-1, dest.stat().st_size):
            return {
                "url": url, "path": str(dest), "bytes": dest.stat().st_size,
                "sha256": sha256_file(dest), "downloaded": False,
            }
        log.info("size changed, re-downloading %s", url)

    _throttle(url)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with SESSION.get(url, stream=True, timeout=REQUEST_TIMEOUT) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
    tmp.replace(dest)
    log.info("downloaded %s (%d bytes)", dest.name, dest.stat().st_size)
    return {
        "url": url, "path": str(dest), "bytes": dest.stat().st_size,
        "sha256": sha256_file(dest), "downloaded": True,
    }
