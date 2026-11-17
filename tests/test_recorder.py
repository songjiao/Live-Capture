"""录制器测试：滑动窗口直播、断档检测、地址过期续录、ffmpeg 后端端到端。"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from live_capture.hls import MediaPlaylist, Segment
from live_capture.recorder import (
    HlsBackend,
    RecordConfig,
    StaticUrl,
    StopFlag,
    probe_duration,
)
from tests.helpers import HlsFixture, HlsServer, run_record

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class FakeClient:
    def download_segment(self, segment: Segment) -> bytes:
        return f"data{segment.sequence}".encode()


class LiveWindowTests(unittest.TestCase):
    def test_follows_sliding_window(self):
        fixture = HlsFixture(total=8, window=3, interval=0.4)
        with HlsServer(fixture) as server, tempfile.TemporaryDirectory() as tmp:
            cfg = RecordConfig(
                out_dir=Path(tmp) / "rec", backend="hls", poll_interval=0.05, remux=False
            )
            result = run_record(StaticUrl(server.url()), cfg, timeout=30)

            self.assertEqual(result.reason, "endlist")
            self.assertEqual(result.segments, 8)
            part = (Path(tmp) / "rec" / "part-000.ts").read_bytes()
            self.assertEqual(part, b"".join(fixture.segments))

            index = [json.loads(line) for line in (Path(tmp) / "rec" / "index.jsonl").read_text().splitlines()]
            self.assertEqual([r["seq"] for r in index if r["event"] == "segment"], list(range(8)))


class GapTests(unittest.TestCase):
    def test_gap_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = RecordConfig(out_dir=Path(tmp) / "rec", backend="hls", remux=False)
            cfg.out_dir.mkdir(parents=True)
            backend = HlsBackend(StaticUrl("http://example.test/x.m3u8"), cfg, StopFlag())
            backend.next_seq = 2
            backend.client = FakeClient()  # type: ignore[assignment]

            playlist = MediaPlaylist(
                url="http://example.test/x.m3u8",
                target_duration=1.0,
                media_sequence=5,
                segments=[
                    Segment(sequence=i, url=f"http://example.test/{i}.ts", duration=1.0)
                    for i in (5, 6, 7)
                ],
            )
            backend._consume(playlist)
            backend.part.close()

            self.assertEqual(backend.next_seq, 8)
            self.assertEqual((cfg.out_dir / "part-000.ts").read_bytes(), b"data5data6data7")
            events = [json.loads(line) for line in (cfg.out_dir / "index.jsonl").read_text().splitlines()]
            gaps = [e for e in events if e["event"] == "gap"]
            self.assertEqual(gaps[0]["from"], 2)
            self.assertEqual(gaps[0]["to"], 5)


class _RefreshResolver:
    """第一次给 url1，之后 refresh() 返回 url2。"""

    def __init__(self, first: str, second: str):
        self.first = first
        self.second = second
        self.refreshed = 0

    def resolve(self) -> str:
        return self.first

    def refresh(self) -> str:
        self.refreshed += 1
        return self.second


class ExpiryTests(unittest.TestCase):
    def test_continues_after_403_with_new_url(self):
        fixture = HlsFixture(total=12, window=3, interval=0.15)
        fixture.forbid_after = {"/live1": 3}
        with HlsServer(fixture) as server, tempfile.TemporaryDirectory() as tmp:
            resolver = _RefreshResolver(server.url("/live1/index.m3u8"), server.url("/live2/index.m3u8"))
            cfg = RecordConfig(
                out_dir=Path(tmp) / "rec",
                backend="hls",
                poll_interval=0.05,
                refresh_wait=5,
                remux=False,
            )
            result = run_record(resolver, cfg, timeout=30)

            self.assertEqual(result.reason, "endlist")
            self.assertEqual(resolver.refreshed, 1)
            # 换地址时会切 part，所以要把所有 part 接起来比对
            recorded = b"".join(p.read_bytes() for p in result.parts)
            self.assertEqual(recorded, b"".join(fixture.segments))
            events = [json.loads(line) for line in (Path(tmp) / "rec" / "index.jsonl").read_text().splitlines()]
            self.assertTrue(any(e["event"] == "url_refresh" for e in events))


@unittest.skipUnless(FFMPEG and FFPROBE, "需要 ffmpeg/ffprobe")
class FfmpegBackendTests(unittest.TestCase):
    def test_record_hls_vod(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
                    "-hls_segment_filename", str(src / "seg%d.ts"), str(src / "index.m3u8"),
                ],
                check=True,
                capture_output=True,
            )
            handler = functools.partial(SimpleHTTPRequestHandler, directory=str(src))
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{httpd.server_address[1]}/index.m3u8"
                cfg = RecordConfig(
                    out_dir=Path(tmp) / "rec",
                    backend="ffmpeg",
                    segment_seconds=2,
                    max_duration=30,
                    remux=True,
                )
                result = run_record(StaticUrl(url), cfg, timeout=60)
            finally:
                httpd.shutdown()
                httpd.server_close()

            self.assertEqual(result.reason, "stream_end")
            self.assertGreaterEqual(len(result.parts), 1)
            self.assertIsNotNone(result.final_mp4)
            self.assertGreater(probe_duration(result.final_mp4), 3.0)


if __name__ == "__main__":
    unittest.main()
