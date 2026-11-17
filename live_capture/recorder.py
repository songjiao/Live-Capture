"""直播录制器。

两种后端：

``hls``
    纯 Python，自己解析 m3u8、按序号增量下载分片、AES 解密、分片丢失时补记断点。
    不依赖 ffmpeg，控制粒度最细，适合 HLS 直播。

``ffmpeg``
    ``ffmpeg -c copy`` 直接拉流，按时间切片。兼容 HLS / HTTP-FLV，省事；
    断流后由外层守护进程用新地址重启，会损失几秒。

``auto``（默认）有 ffmpeg 就用 ffmpeg，否则用 hls。

两者都会输出 ``part-*.ts`` 分片 + 索引文件，便于后续做时间轴对齐、
抽帧和事件标注；结束时可用 ffmpeg 无损转封装成 mp4。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .hls import HlsClient, MasterPlaylist, MediaPlaylist, Segment, select_variant
from .util import (
    HttpClient,
    HttpError,
    append_jsonl,
    atomic_write_text,
    human_bytes,
    human_duration,
    redact_url,
)

log = logging.getLogger("wxcap.recorder")


# --------------------------------------------------------------------------- #
# 地址来源：手动给、从文件读（DLNA 捕获写进去）、跑命令拿
# --------------------------------------------------------------------------- #


class UrlResolver(Protocol):
    def resolve(self) -> str | None: ...
    def refresh(self) -> str | None: ...


class StaticUrl:
    def __init__(self, url: str):
        self.url = url

    def resolve(self) -> str | None:
        return self.url

    def refresh(self) -> str | None:
        return self.url


class FileUrl:
    """从文件读地址；DLNA 模块每捕获到新地址就会写这个文件。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> str | None:
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
        except FileNotFoundError:
            pass
        return None

    def resolve(self) -> str | None:
        return self._read()

    def refresh(self) -> str | None:
        return self._read()


class CommandUrl:
    """跑一条命令，把 stdout 里的 URL 当作地址（对接自定义抓流脚本）。"""

    def __init__(self, command: str, timeout: float = 120.0):
        self.command = command
        self.timeout = timeout

    def _run(self) -> str | None:
        try:
            proc = subprocess.run(
                self.command, shell=True, capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired:
            log.warning("取流命令超时: %s", self.command)
            return None
        if proc.returncode != 0:
            log.warning("取流命令退出码 %s: %s", proc.returncode, proc.stderr.strip()[:300])
            return None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith(("http://", "https://")):
                return line
        return None

    def resolve(self) -> str | None:
        return self._run()

    def refresh(self) -> str | None:
        return self._run()


# --------------------------------------------------------------------------- #
# 配置与结果
# --------------------------------------------------------------------------- #


@dataclass
class RecordConfig:
    out_dir: Path
    quality: str = "best"
    start: str = "window"  # window=从当前窗口最早的分片开始；edge=从最新分片开始
    backend: str = "auto"  # auto | hls | ffmpeg
    segment_seconds: float = 600.0
    max_duration: float = 0.0  # 0 表示不限制
    max_stall: float = 120.0
    refresh_wait: float = 60.0  # 地址失效后等新地址的最长时间（重新投屏需要时间）
    poll_interval: float = 0.0  # 0 表示按 target_duration 自适应
    headers: dict[str, str] = field(default_factory=dict)
    remux: bool = True
    ffmpeg_bin: str = "ffmpeg"
    restart_delay: float = 5.0
    max_restarts: int = 30
    split_on_discontinuity: bool = True


@dataclass
class RecordResult:
    out_dir: Path
    backend: str
    reason: str
    duration_seconds: float
    segments: int
    bytes_written: int
    parts: list[Path] = field(default_factory=list)
    final_mp4: Path | None = None
    restarts: int = 0

    def summary(self) -> str:
        lines = [
            f"输出目录 : {self.out_dir}",
            f"后端     : {self.backend}",
            f"结束原因 : {self.reason}",
            f"时长     : {human_duration(self.duration_seconds)}",
            f"分片数   : {self.segments}",
            f"大小     : {human_bytes(self.bytes_written)}",
            f"文件     : {len(self.parts)} 个 part",
        ]
        if self.restarts:
            lines.append(f"重连次数 : {self.restarts}")
        if self.final_mp4:
            lines.append(f"成片     : {self.final_mp4}")
        return "\n".join(lines)


class _Stop(Exception):
    """内部使用：请求录制循环优雅退出。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class StopFlag:
    def __init__(self):
        self._event = threading.Event()
        self._installed = False

    def install(self) -> None:
        if self._installed or threading.current_thread() is not threading.main_thread():
            return
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)
        self._installed = True

    def _handle(self, signum, frame):  # noqa: ARG002
        if self._event.is_set():
            raise KeyboardInterrupt
        log.info("收到信号 %s，正在收尾（再按一次强制退出）…", signum)
        self._event.set()

    @property
    def set(self) -> bool:
        return self._event.is_set()


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def ffmpeg_available(ffmpeg_bin: str = "ffmpeg") -> bool:
    return shutil.which(ffmpeg_bin) is not None


def probe_duration(path: Path) -> float:
    if not path.exists() or not shutil.which("ffprobe"):
        return 0.0
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(proc.stdout.strip() or 0)
    except (ValueError, subprocess.SubprocessError):
        return 0.0


def remux_parts(parts: list[Path], out_path: Path, ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """把若干 .ts 分片无损拼接转封装为 mp4。"""
    parts = [p for p in parts if p.exists() and p.stat().st_size > 0]
    if not parts:
        return None
    if not ffmpeg_available(ffmpeg_bin):
        log.warning("没找到 ffmpeg，保留 TS 分片（TS 本身可直接播放/分析）")
        return None
    list_file = out_path.parent / ".concat.txt"
    list_file.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8"
    )
    cmd = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-movflags", "+faststart", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if proc.returncode != 0:
        log.warning("转封装失败（TS 分片仍然可用）: %s", proc.stderr.strip()[:400])
        return None
    log.info("成片: %s", out_path)
    return out_path


class _PartWriter:
    """按时间滚动写 part-000.ts / part-001.ts …，写坏也不影响已完成的 part。"""

    def __init__(self, out_dir: Path, segment_seconds: float):
        self.out_dir = out_dir
        self.segment_seconds = max(1.0, segment_seconds)
        self.index = 0
        self.parts: list[Path] = []
        self._fh = None
        self._started = 0.0
        self.bytes_written = 0
        self._part_bytes = 0

    def _open(self) -> None:
        final = self.out_dir / f"part-{self.index:03d}.ts"
        self._fh = final.with_suffix(".ts.part").open("wb")
        self.parts.append(final)
        self._started = time.monotonic()
        self._part_bytes = 0

    def write(self, data: bytes) -> None:
        if self._fh is None:
            self._open()
        self._fh.write(data)
        self.bytes_written += len(data)
        self._part_bytes += len(data)

    def should_roll(self, discontinuity: bool = False) -> bool:
        if self._fh is None:
            return False
        if discontinuity:
            return True
        return time.monotonic() - self._started >= self.segment_seconds

    def roll(self) -> None:
        if self._fh is None:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None
        final = self.parts[-1]
        tmp = final.with_suffix(".ts.part")
        if tmp.exists():
            os.replace(tmp, final)
        log.info("已保存 %s (%s)", final.name, human_bytes(self._part_bytes))
        self.index += 1

    def close(self) -> None:
        self.roll()


# --------------------------------------------------------------------------- #
# 纯 Python HLS 后端
# --------------------------------------------------------------------------- #


class HlsBackend:
    def __init__(self, resolver: UrlResolver, cfg: RecordConfig, stop: StopFlag):
        self.resolver = resolver
        self.cfg = cfg
        self.stop = stop
        self.http = HttpClient(headers=cfg.headers)
        self.client = HlsClient(self.http)
        self.entry_url: str | None = None
        self.media_url: str | None = None
        self.next_seq: int | None = None
        self.segments = 0
        self.duration = 0.0
        self.gaps = 0
        self.refreshes = 0
        self.reason = "stopped"
        self.last_progress = time.monotonic()
        self.part = _PartWriter(cfg.out_dir, cfg.segment_seconds)
        self.index_path = cfg.out_dir / "index.jsonl"

    # -- 打开直播 ---------------------------------------------------------- #

    def _open(self, url: str) -> MediaPlaylist:
        playlist = self.client.fetch(url)
        if isinstance(playlist, MasterPlaylist):
            variant = select_variant(playlist, self.cfg.quality)
            log.info("选定码流: %s", variant.describe())
            self.media_url = variant.url
            playlist = self.client.fetch_media(variant.url)
        else:
            self.media_url = url
        if not playlist.segments:
            raise ValueError("播放列表里没有分片，可能还没开播")
        self.entry_url = url
        previous = self.next_seq
        if (
            previous is not None
            and playlist.first_sequence <= previous <= playlist.last_sequence + 1
        ):
            # 换地址后接得上，就接着录，避免重复下载已经录过的分片
            self.next_seq = previous
            log.info("续录: 从序号 %d 继续（目标时长 %.1fs）", previous, playlist.target_duration)
        else:
            if previous is not None:
                log.warning("新地址的分片序号对不上，按新直播从头/从窗口开始")
            if self.cfg.start == "edge" and previous is None:
                self.next_seq = playlist.last_sequence
            else:
                self.next_seq = playlist.first_sequence
            log.info(
                "开始录制: 目标时长 %.1fs, 窗口 %d 个分片, 从序号 %d 开始",
                playlist.target_duration, len(playlist.segments), self.next_seq,
            )
        return playlist

    # -- 主循环 ------------------------------------------------------------ #

    def run(self) -> RecordResult:
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._loop()
        finally:
            self.part.close()
            self._write_meta()

        parts = [p for p in self.part.parts if p.exists()]
        final_mp4 = None
        if self.cfg.remux and parts:
            final_mp4 = remux_parts(parts, self.cfg.out_dir / "recording.mp4", self.cfg.ffmpeg_bin)
        duration = self.duration or (probe_duration(final_mp4) if final_mp4 else 0.0)
        return RecordResult(
            out_dir=self.cfg.out_dir,
            backend="hls",
            reason=self.reason,
            duration_seconds=duration,
            segments=self.segments,
            bytes_written=self.part.bytes_written,
            parts=parts,
            final_mp4=final_mp4,
        )

    def _loop(self) -> None:
        started = time.monotonic()
        failures = 0
        try:
            while True:
                if self.stop.set:
                    self.reason = "user_stop"
                    return
                if self.cfg.max_duration and time.monotonic() - started >= self.cfg.max_duration:
                    self.reason = "max_duration"
                    return

                if self.media_url is None or self.next_seq is None:
                    if not self._try_open():
                        failures += 1
                        if self._give_up(failures):
                            return
                        time.sleep(min(2 ** min(failures, 4), 15))
                        self.resolver.refresh()
                        continue
                    failures = 0
                    continue

                if time.monotonic() - self.last_progress > self.cfg.max_stall:
                    log.warning("%.0fs 没有新分片，尝试重新取流地址", self.cfg.max_stall)
                    if not self._refresh("stall"):
                        self.reason = "stalled"
                        return
                    continue

                try:
                    fresh = self.client.fetch_media(self.media_url)
                except HttpError as exc:
                    failures += 1
                    log.warning(
                        "拉播放列表失败 HTTP %s（第 %d 次）", exc.status, failures
                    )
                    if exc.status in (401, 403, 404, 410) and self._refresh(f"http_{exc.status}"):
                        continue
                    if self._give_up(failures):
                        self.reason = f"http_{exc.status}"
                        return
                    time.sleep(min(2 ** min(failures, 4), 15))
                    continue
                except Exception as exc:  # noqa: BLE001 - 网络抖动等
                    failures += 1
                    log.warning("拉播放列表异常: %s（第 %d 次）", exc, failures)
                    if self._give_up(failures):
                        self.reason = "unreachable"
                        return
                    time.sleep(min(2 ** min(failures, 4), 15))
                    continue

                failures = 0
                if self._consume(fresh):
                    self.reason = "endlist"
                    return
                time.sleep(self._sleep_seconds(fresh))
        except _Stop:
            self.reason = "user_stop"

    def _try_open(self) -> bool:
        # 已经刷新过地址时优先用新地址，避免又重新拿回旧地址
        url = self.entry_url or self.resolver.resolve()
        if not url:
            log.warning("还没有直播地址")
            return False
        try:
            self._open(url)
            self.last_progress = time.monotonic()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("打开直播失败: %s", exc)
            return False

    def _give_up(self, failures: int) -> bool:
        return time.monotonic() - self.last_progress > self.cfg.max_stall * 2

    def _consume(self, playlist: MediaPlaylist) -> bool:
        """下载新分片，返回 False 表示还没到结尾。"""
        assert self.next_seq is not None
        available = [s for s in playlist.segments if s.sequence >= self.next_seq]
        if not available:
            return playlist.endlist and playlist.last_sequence < self.next_seq

        earliest = available[0].sequence
        if earliest > self.next_seq:
            gap = earliest - self.next_seq
            self.gaps += 1
            log.warning("丢掉 %d 个分片（%d → %d）", gap, self.next_seq, earliest)
            append_jsonl(
                self.index_path,
                {"t": time.time(), "event": "gap", "from": self.next_seq, "to": earliest},
            )
            self.next_seq = earliest

        for segment in available:
            if self.stop.set:
                raise _Stop
            self._download(segment)
        return playlist.endlist

    def _download(self, segment: Segment) -> None:
        try:
            data = self.client.download_segment(segment)
        except HttpError as exc:
            log.warning("分片 %d 下载失败 HTTP %s，跳过", segment.sequence, exc.status)
            append_jsonl(
                self.index_path,
                {"t": time.time(), "event": "drop", "seq": segment.sequence, "status": exc.status},
            )
            self.next_seq = segment.sequence + 1
            return
        except Exception as exc:  # noqa: BLE001 - 单个分片坏掉不该终止整场录制
            log.warning("分片 %d 处理失败: %s，跳过", segment.sequence, exc)
            self.next_seq = segment.sequence + 1
            return

        if segment.discontinuity and self.cfg.split_on_discontinuity:
            self.part.roll()
        self.part.write(data)
        self.segments += 1
        self.duration += segment.duration
        self.next_seq = segment.sequence + 1
        self.last_progress = time.monotonic()
        append_jsonl(
            self.index_path,
            {
                "t": time.time(),
                "event": "segment",
                "seq": segment.sequence,
                "duration": segment.duration,
                "bytes": len(data),
                "part": self.part.parts[-1].name if self.part.parts else "",
                "discontinuity": segment.discontinuity,
            },
        )
        if self.segments % 15 == 0 or self.segments == 1:
            log.info(
                "已录 %s / %d 分片 / %s",
                human_duration(self.duration), self.segments, human_bytes(self.part.bytes_written),
            )
        if self.part.should_roll():
            self.part.roll()

    # -- 换地址 ------------------------------------------------------------ #

    def _refresh(self, reason: str) -> bool:
        self.refreshes += 1
        log.info("重新获取直播地址（第 %d 次，原因 %s）", self.refreshes, reason)
        if reason == "stall":
            # 没有新分片通常意味着这场直播结束了，不必再等新地址
            new_url = self.resolver.refresh()
            if not new_url or new_url == self.entry_url:
                log.warning("没有取到新地址")
                return False
        else:
            # 地址过期：用户可能要重新投屏一次，给一段等待时间
            new_url = None
            deadline = time.monotonic() + max(0.0, self.cfg.refresh_wait)
            warned = False
            while time.monotonic() < deadline and not self.stop.set:
                candidate = self.resolver.refresh()
                if candidate and candidate != self.entry_url:
                    new_url = candidate
                    break
                if not warned:
                    log.warning("地址已失效，等待重新投屏/更新地址（最多 %.0fs）…", self.cfg.refresh_wait)
                    warned = True
                time.sleep(2)
            if not new_url:
                log.warning("等不到新地址，停止录制（已录部分不受影响）")
                return False
        append_jsonl(self.index_path, {"t": time.time(), "event": "url_refresh", "reason": reason})
        self.entry_url = new_url
        self.media_url = None
        # 注意：不清空 next_seq —— 新地址如果还是同一场直播，序号能接上就续录
        self.last_progress = time.monotonic()
        self.part.roll()
        return True

    def _sleep_seconds(self, playlist: MediaPlaylist) -> float:
        if self.cfg.poll_interval:
            return self.cfg.poll_interval
        return max(1.0, min(playlist.target_duration / 2.0, 5.0))

    def _write_meta(self) -> None:
        meta = {
            "backend": "hls",
            "reason": self.reason,
            "finished_at": _now_iso(),
            "duration_seconds": round(self.duration, 2),
            "segments": self.segments,
            "gap_events": self.gaps,
            "url_refreshes": self.refreshes,
            "bytes": self.part.bytes_written,
            "parts": [p.name for p in self.part.parts if p.exists()],
            "entry_url": redact_url(self.entry_url) if self.entry_url else None,
        }
        atomic_write_text(
            self.cfg.out_dir / "recording.json", json.dumps(meta, ensure_ascii=False, indent=2)
        )


# --------------------------------------------------------------------------- #
# ffmpeg 后端
# --------------------------------------------------------------------------- #


class FfmpegBackend:
    def __init__(self, resolver: UrlResolver, cfg: RecordConfig, stop: StopFlag):
        self.resolver = resolver
        self.cfg = cfg
        self.stop = stop
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self.reason = "stopped"
        self.bytes_written = 0
        self.events_path = cfg.out_dir / "events.jsonl"

    def _parts(self) -> list[Path]:
        return sorted(self.cfg.out_dir.glob("part-*.ts"))

    def _next_index(self) -> int:
        indexes = []
        for path in self._parts():
            try:
                indexes.append(int(path.stem.split("-")[-1]))
            except ValueError:
                continue
        return max(indexes) + 1 if indexes else 0

    def run(self) -> RecordResult:
        if not ffmpeg_available(self.cfg.ffmpeg_bin):
            raise RuntimeError("没有找到 ffmpeg，请安装或改用 --backend hls")
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        url = self.resolver.resolve()
        if not url:
            raise RuntimeError("没有拿到直播地址")
        try:
            while True:
                if self.stop.set:
                    self.reason = "user_stop"
                    break
                if self.cfg.max_duration and time.monotonic() - started >= self.cfg.max_duration:
                    self.reason = "max_duration"
                    break
                log.info("启动 ffmpeg 录制: %s", redact_url(url))
                code = self._run_once(url)
                if self.stop.set:
                    self.reason = "user_stop"
                    break
                if code == 0:
                    self.reason = "stream_end"
                    break
                self.restarts += 1
                append_jsonl(
                    self.events_path,
                    {"t": time.time(), "event": "restart", "code": code, "count": self.restarts},
                )
                if self.restarts > self.cfg.max_restarts:
                    self.reason = f"too_many_restarts({self.restarts})"
                    break
                delay = min(self.cfg.restart_delay * (1.5 ** min(self.restarts - 1, 6)), 60.0)
                log.warning("ffmpeg 退出码 %s，%.0fs 后重试", code, delay)
                time.sleep(delay)
                new_url = self.resolver.refresh()
                if new_url:
                    url = new_url
        finally:
            self._terminate()

        parts = self._parts()
        self.bytes_written = sum(p.stat().st_size for p in parts)
        final_mp4 = None
        if self.cfg.remux and parts:
            final_mp4 = remux_parts(parts, self.cfg.out_dir / "recording.mp4", self.cfg.ffmpeg_bin)
        duration = probe_duration(final_mp4) if final_mp4 else 0.0
        if not duration:
            duration = sum(probe_duration(p) for p in parts)
        meta = {
            "backend": "ffmpeg",
            "reason": self.reason,
            "restarts": self.restarts,
            "duration_seconds": round(duration, 2),
            "bytes": self.bytes_written,
            "parts": [p.name for p in parts],
            "finished_at": _now_iso(),
        }
        atomic_write_text(
            self.cfg.out_dir / "recording.json", json.dumps(meta, ensure_ascii=False, indent=2)
        )
        return RecordResult(
            out_dir=self.cfg.out_dir,
            backend="ffmpeg",
            reason=self.reason,
            duration_seconds=duration,
            segments=len(parts),
            bytes_written=self.bytes_written,
            parts=parts,
            final_mp4=final_mp4,
            restarts=self.restarts,
        )

    def _run_once(self, url: str) -> int:
        next_index = self._next_index()
        cmd = [
            self.cfg.ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10", "-rw_timeout", "15000000",
            "-user_agent", self.cfg.headers.get("User-Agent", "Mozilla/5.0"),
        ]
        extra_headers = {k: v for k, v in self.cfg.headers.items() if k.lower() != "user-agent"}
        if extra_headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in extra_headers.items())]
        cmd += [
            "-i", url,
            "-c", "copy", "-map", "0:v?", "-map", "0:a?",
            "-f", "segment",
            "-segment_time", str(int(self.cfg.segment_seconds)),
            "-segment_start_number", str(next_index),
            "-reset_timestamps", "1",
            "-segment_format", "mpegts",
            str(self.cfg.out_dir / "part-%03d.ts"),
        ]
        log.debug("ffmpeg: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        try:
            while self.proc.poll() is None:
                if self.stop.set:
                    self._terminate()
                    break
                time.sleep(0.5)
        finally:
            code = self.proc.returncode if self.proc else 0
        return code if code is not None else 0

    def _terminate(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def record(resolver: UrlResolver, cfg: RecordConfig, stop: StopFlag | None = None) -> RecordResult:
    stop = stop or StopFlag()
    stop.install()
    backend = cfg.backend
    if backend == "auto":
        # m3u8 用纯 Python 后端：分片级控制、断档可见、不依赖 ffmpeg；
        # FLV / 未知格式交给 ffmpeg。
        url = (resolver.resolve() or "").lower()
        if ".m3u8" in url:
            backend = "hls"
        elif ffmpeg_available(cfg.ffmpeg_bin):
            backend = "ffmpeg"
        else:
            backend = "hls"
    log.info("录制后端: %s", backend)
    if backend == "ffmpeg":
        return FfmpegBackend(resolver, cfg, stop).run()
    if backend == "hls":
        return HlsBackend(resolver, cfg, stop).run()
    raise ValueError(f"未知后端: {cfg.backend}")
