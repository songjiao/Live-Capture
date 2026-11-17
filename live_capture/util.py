"""HTTP、日志、文件写入等公共小工具。"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 MicroMessenger/8.0"
)

#: 日志里需要打码的 query 参数（视频号地址普遍带 token，泄露后他人可直接拉流）
SENSITIVE_KEYS = {
    "token",
    "encfilekey",
    "encver",
    "sign",
    "signature",
    "txsecret",
    "txsign",
    "auth",
    "authkey",
    "key",
    "secret",
    "expires",
}

log = logging.getLogger("wxcap")


def redact_url(url: str) -> str:
    """把 URL 里的敏感参数替换成 ***，方便安全地打印到日志/终端。"""
    try:
        parts = urllib.parse.urlsplit(url)
        if not parts.query:
            return url
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        masked = []
        for k, v in pairs:
            if k.lower() in SENSITIVE_KEYS and v:
                masked.append((k, "***"))
            else:
                masked.append((k, v))
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path,
             urllib.parse.urlencode(masked), parts.fragment)
        )
    except Exception:  # pragma: no cover - 防御性兜底
        return "<unparsable-url>"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class HttpError(Exception):
    """带 HTTP 状态码的下载错误，便于上层判断 403/404 是否该重取地址。"""

    def __init__(self, url: str, status: int, message: str = ""):
        super().__init__(f"HTTP {status} {message} for {redact_url(url)}")
        self.url = url
        self.status = status


class HttpClient:
    """带重试的最小 HTTP 客户端（标准库实现，不引入 requests）。"""

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 1.5,
    ):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            # 不请求压缩，省得自己解 gzip
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if headers:
            self.headers.update(headers)

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> bytes:
        merged = dict(self.headers)
        if headers:
            merged.update(headers)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, data=data, headers=merged, method=method)
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                # 4xx（除 429）重试没有意义，直接把状态码抛给调用方
                if exc.code < 500 and exc.code != 429:
                    raise HttpError(url, exc.code, exc.reason) from exc
                last_error = HttpError(url, exc.code, exc.reason)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = exc
            if attempt < self.retries - 1:
                time.sleep(self.backoff * (2**attempt) * (0.8 + 0.4 * random.random()))
        if isinstance(last_error, HttpError):
            raise last_error
        raise HttpError(url, 0, str(last_error)) from last_error

    def get_text(self, url: str, **kwargs) -> str:
        return self.request(url, **kwargs).decode("utf-8", errors="replace")

    def get_bytes(self, url: str, **kwargs) -> bytes:
        return self.request(url, **kwargs)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # urllib 的 debug 日志太吵，压掉
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_headers(items: list[str] | None) -> dict[str, str]:
    """把 ``-H 'Referer: https://...'`` 形式解析成 dict。"""
    out: dict[str, str] = {}
    for item in items or []:
        if ":" not in item:
            raise ValueError(f"header 格式应为 'Key: Value'，收到: {item!r}")
        key, value = item.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def safe_filename(name: str, fallback: str = "recording") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("._")
    return cleaned or fallback
