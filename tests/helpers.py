"""测试用的小型 HLS 直播服务器（滑动窗口、可模拟 403）。"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def default_payload(index: int) -> bytes:
    # 可区分、可校验的假分片
    return (f"SEG{index:03d}|".encode() * 40) + bytes([index % 256])


class HlsFixture:
    def __init__(self, total: int = 9, window: int = 3, interval: float = 0.15, end_after: bool = True):
        self.total = total
        self.window = window
        self.interval = interval
        self.end_after = end_after
        self.start = time.monotonic()
        self.segments = [default_payload(i) for i in range(total)]
        self.forbidden_prefixes: set[str] = set()
        # {路径前缀: 允许的 m3u8 请求次数}，用来模拟 token 过期后返回 403
        self.forbid_after: dict[str, int] = {}
        self.playlist_counts: dict[str, int] = {}
        self.requested_paths: list[str] = []
        self.lock = threading.Lock()

    @property
    def visible(self) -> int:
        elapsed = time.monotonic() - self.start
        return min(self.total, self.window + int(elapsed / self.interval))

    def playlist_text(self) -> str:
        visible = self.visible
        first = max(0, visible - self.window)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:1",
            f"#EXT-X-MEDIA-SEQUENCE:{first}",
        ]
        for i in range(first, visible):
            lines += ["#EXTINF:1.000,", f"seg{i}.ts"]
        if self.end_after and visible >= self.total:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def do_GET(self):  # noqa: N802
        fixture: HlsFixture = self.server.fixture  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        with fixture.lock:
            fixture.requested_paths.append(path)
        if any(path.startswith(p) for p in fixture.forbidden_prefixes):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.endswith(".m3u8"):
            prefix = path.rsplit("/", 1)[0]
            with fixture.lock:
                count = fixture.playlist_counts.get(prefix, 0) + 1
                fixture.playlist_counts[prefix] = count
            limit = fixture.forbid_after.get(prefix)
            if limit is not None and count > limit:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = fixture.playlist_text().encode()
            self._send(200, body, "application/vnd.apple.mpegurl")
            return
        if path.endswith(".ts"):
            name = path.rsplit("/", 1)[-1].removesuffix(".ts")
            try:
                index = int(name.removeprefix("seg"))
                payload = fixture.segments[index]
            except (ValueError, IndexError):
                self._send(404, b"", "text/plain")
                return
            self._send(200, payload, "video/mp2t")
            return
        self._send(404, b"", "text/plain")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class HlsServer:
    def __init__(self, fixture: HlsFixture):
        self.fixture = fixture
        self.httpd = _Server(("127.0.0.1", 0), _Handler)
        self.httpd.fixture = fixture  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str = "/live/index.m3u8") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def run_record(resolver, cfg, timeout: float = 90.0):
    """在子线程里跑录制（子线程里不会安装信号处理器），返回 RecordResult。"""
    from live_capture.recorder import record

    box: dict = {}

    def target():
        try:
            box["result"] = record(resolver, cfg)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(f"录制在 {timeout:.0f}s 内没有结束")
    if "error" in box:
        raise box["error"]
    return box["result"]
