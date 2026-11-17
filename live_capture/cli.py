"""命令行入口：``python -m live_capture`` / ``wxcap``。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .dlna import CapturedStream, DlnaCapture
from .hls import probe as hls_probe
from .recorder import (
    CommandUrl,
    FileUrl,
    RecordConfig,
    StaticUrl,
    ffmpeg_available,
    record,
    remux_parts,
)
from .util import HttpClient, human_bytes, parse_headers, setup_logging

log = logging.getLogger("wxcap.cli")

DEFAULT_URL_FILE = "captures/current_url.txt"
DEFAULT_HISTORY = "captures/history.jsonl"


def _build_http_client(args) -> HttpClient:
    return HttpClient(headers=parse_headers(getattr(args, "header", None)))


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


def cmd_probe(args) -> int:
    http = _build_http_client(args)
    try:
        info = hls_probe(args.url, http, quality=args.quality)
    except Exception as exc:  # noqa: BLE001
        log.error("探测失败: %s", exc)
        return 1
    if info["kind"] == "master":
        print(f"码流列表（共 {len(info['variants'])} 路）:")
        for item in info["variants"]:
            mark = "*" if item["resolution"] == info["selected"]["resolution"] else " "
            print(
                f" {mark} [{item['index']}] {item['resolution'] or '?':>10} "
                f"{item['frame_rate'] or '?':>5}fps "
                f"{item['bandwidth'] // 1000:>6}kbps  {item['codecs']}"
            )
        selected = info["selected"]
        print(
            f"\n默认选择: {selected['resolution']} {selected['bandwidth'] // 1000}kbps "
            f"{selected['frame_rate']}fps"
        )
    else:
        print(f"单码流 m3u8: {info['segments_in_window']} 个分片，"
              f"目标时长 {info['target_duration']}s，"
              f"{'已结束' if info['endlist'] else '直播中'}，"
              f"{'加密' if info.get('encrypted') else '未加密'}")
    return 0


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #


def _wait_for_url(path: Path, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = FileUrl(path).resolve()
        if url:
            return url
        time.sleep(1.0)
    return None


def _make_resolver(args):
    sources = [bool(args.url), bool(args.url_file), bool(args.url_cmd)]
    if sum(sources) != 1:
        raise SystemExit("请且只提供一种地址来源：位置参数 URL、--url-file 或 --url-cmd")
    if args.url:
        return StaticUrl(args.url)
    if args.url_file:
        path = Path(args.url_file)
        if args.wait:
            log.info("等待地址文件 %s（最多 %.0fs）…", path, args.wait)
            if not _wait_for_url(path, args.wait):
                raise SystemExit(f"等待 {args.wait:.0f}s 仍没有地址：{path}")
        return FileUrl(path)
    return CommandUrl(args.url_cmd)


def cmd_record(args) -> int:
    try:
        resolver = _make_resolver(args)
    except SystemExit as exc:
        log.error("%s", exc)
        return 2

    out_dir = Path(args.out or Path("recordings") / time.strftime("%Y%m%d-%H%M%S"))
    cfg = RecordConfig(
        out_dir=out_dir,
        quality=args.quality,
        start=args.start,
        backend=args.backend,
        segment_seconds=args.segment_seconds,
        max_duration=args.max_duration,
        max_stall=args.max_stall,
        refresh_wait=args.refresh_wait,
        poll_interval=args.poll_interval,
        headers=parse_headers(args.header),
        remux=not args.no_remux,
        ffmpeg_bin=args.ffmpeg,
        split_on_discontinuity=not args.keep_discontinuity,
    )
    if not ffmpeg_available(cfg.ffmpeg_bin) and cfg.remux:
        log.warning("未找到 ffmpeg，将只保留 TS 分片（不影响录制）")
        cfg.remux = False
    try:
        result = record(resolver, cfg)
    except KeyboardInterrupt:
        log.warning("强制退出")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.error("录制失败: %s", exc)
        return 1
    print("\n" + result.summary())
    return 0


# --------------------------------------------------------------------------- #
# dlna
# --------------------------------------------------------------------------- #


def cmd_dlna(args) -> int:
    url_file = Path(args.url_file)
    history = Path(args.history)
    url_file.parent.mkdir(parents=True, exist_ok=True)
    history.parent.mkdir(parents=True, exist_ok=True)

    first_capture = threading.Event()
    state = {"first": None}

    def on_capture(stream: CapturedStream) -> None:
        text = (
            f"# captured {time.strftime('%Y-%m-%d %H:%M:%S')} via {stream.action}\n"
            f"{stream.url}\n"
        )
        url_file.write_text(text, encoding="utf-8")
        with history.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": stream.at, "url": stream.url, "action": stream.action},
                                ensure_ascii=False) + "\n")
        print(stream.url, flush=True)
        state["first"] = stream
        first_capture.set()

    capture = DlnaCapture(name=args.name, http_port=args.http_port,
                          ssdp_port=args.ssdp_port, on_capture=on_capture,
                          lan_ip=args.lan_ip)
    try:
        capture.start()
    except OSError as exc:
        log.error("启动失败: %s", exc)
        return 1

    print(f"设备名: {args.name}")
    print(f"本机 IP: {capture.lan_ip}（多网卡/开 VPN 选错时用 --lan-ip 指定）")
    print(f"描述地址: {capture.location}")
    print(f"地址会写入: {url_file}")
    print("请用手机微信打开视频号直播 → 右上角「…」→ 投屏，选择该设备。\n"
          "（手机与电脑需在同一 WiFi；iPhone 微信可能走 AirPlay，本工具看不到）", flush=True)

    try:
        if args.once:
            if not first_capture.wait(timeout=args.timeout):
                log.error("超时未捕获到投屏地址")
                return 1
            return 0
        if args.record:
            log.info("等待投屏后自动开始录制，输出目录: %s", args.record)
            if not first_capture.wait(timeout=args.timeout):
                log.error("超时未捕获到投屏地址")
                return 1
            cfg = RecordConfig(
                out_dir=Path(args.record),
                quality=args.quality,
                backend=args.backend,
                segment_seconds=args.segment_seconds,
                max_duration=args.max_duration,
                refresh_wait=args.refresh_wait,
                headers=parse_headers(args.header),
                remux=not args.no_remux,
                ffmpeg_bin=args.ffmpeg,
            )
            # 主线程跑录制：Ctrl-C 由录制器的信号处理接管，DLNA 线程继续待命，
            # 这样中途重新投屏（换新地址）也能被记录到 url_file 里。
            try:
                result = record(FileUrl(url_file), cfg)
            except KeyboardInterrupt:
                log.warning("强制退出")
                return 130
            except Exception as exc:  # noqa: BLE001
                log.error("录制失败: %s", exc)
                return 1
            print("\n" + result.summary())
            return 0
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0
    finally:
        capture.stop()


# --------------------------------------------------------------------------- #
# remux
# --------------------------------------------------------------------------- #


def cmd_remux(args) -> int:
    directory = Path(args.dir)
    parts = sorted(directory.glob("part-*.ts"))
    if not parts:
        log.error("%s 下没有 part-*.ts", directory)
        return 1
    total = sum(p.stat().st_size for p in parts)
    log.info("发现 %d 个分片，共 %s", len(parts), human_bytes(total))
    out = directory / (args.output or "recording.mp4")
    result = remux_parts(parts, out, args.ffmpeg)
    if not result:
        return 1
    print(f"已生成: {result}")
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wxcap",
        description="微信视频号直播流捕获与录制（个人研究用途，请勿传播录制内容）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"wxcap {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="探测直播地址的码流/分片信息")
    p_probe.add_argument("url")
    p_probe.add_argument("--quality", default="best", help="best/worst/1080/720/序号")
    p_probe.add_argument("-H", "--header", action="append", help="额外请求头，如 'Referer: https://...'")
    p_probe.set_defaults(func=cmd_probe)

    p_rec = sub.add_parser("record", help="录制直播（支持 m3u8 / FLV）")
    p_rec.add_argument("url", nargs="?", help="直播地址（m3u8/flv 直链）")
    p_rec.add_argument("--url-file", help="从文件读取地址（配合 dlna 使用）")
    p_rec.add_argument("--url-cmd", help="执行命令并从 stdout 读取地址")
    p_rec.add_argument("--wait", type=float, default=0, help="等待地址文件出现的秒数")
    p_rec.add_argument("-o", "--out", help="输出目录（默认 recordings/<时间戳>）")
    p_rec.add_argument("--quality", default="best", help="best/worst/1080/720/序号")
    p_rec.add_argument("--start", choices=["window", "edge"], default="window",
                       help="从当前窗口最早分片开始（window）还是最新分片（edge）")
    p_rec.add_argument("--backend", choices=["auto", "hls", "ffmpeg"], default="auto")
    p_rec.add_argument("--segment-seconds", type=float, default=600, help="单个 part 的时长上限")
    p_rec.add_argument("--max-duration", type=float, default=0, help="最长录制秒数，0=不限")
    p_rec.add_argument("--max-stall", type=float, default=120, help="多少秒没有新分片判定为断流")
    p_rec.add_argument("--refresh-wait", type=float, default=60,
                       help="地址失效后等待重新投屏/新地址的秒数")
    p_rec.add_argument("--poll-interval", type=float, default=0, help="轮询播放列表的间隔秒数")
    p_rec.add_argument("--keep-discontinuity", action="store_true",
                       help="遇到 EXT-X-DISCONTINUITY 时不切分 part")
    p_rec.add_argument("--no-remux", action="store_true", help="结束时不做 mp4 转封装")
    p_rec.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径")
    p_rec.add_argument("-H", "--header", action="append", help="额外请求头")
    p_rec.set_defaults(func=cmd_record)

    p_dlna = sub.add_parser("dlna", help="伪装成电视，捕获投屏过来的直播地址")
    p_dlna.add_argument("--name", default="wxcap 电视", help="投屏列表里显示的名字")
    p_dlna.add_argument("--url-file", default=DEFAULT_URL_FILE, help="捕获到的地址写入哪个文件")
    p_dlna.add_argument("--history", default=DEFAULT_HISTORY, help="历史捕获记录")
    p_dlna.add_argument("--http-port", type=int, default=0, help="HTTP 端口，0=随机")
    p_dlna.add_argument("--ssdp-port", type=int, default=1900, help="SSDP 监听端口，默认 1900；仅测试用，改了手机就搜不到")
    p_dlna.add_argument("--lan-ip", help="手动指定本机局域网 IP（多网卡/开 VPN 时用）")
    p_dlna.add_argument("--once", action="store_true", help="捕获到第一个地址后退出")
    p_dlna.add_argument("--timeout", type=float, default=1800, help="等待投屏的超时秒数")
    p_dlna.add_argument("--record", metavar="DIR", help="捕获到地址后自动开始录制到 DIR")
    p_dlna.add_argument("--quality", default="best")
    p_dlna.add_argument("--backend", choices=["auto", "hls", "ffmpeg"], default="auto")
    p_dlna.add_argument("--segment-seconds", type=float, default=600)
    p_dlna.add_argument("--max-duration", type=float, default=0)
    p_dlna.add_argument("--refresh-wait", type=float, default=60)
    p_dlna.add_argument("--no-remux", action="store_true")
    p_dlna.add_argument("--ffmpeg", default="ffmpeg")
    p_dlna.add_argument("-H", "--header", action="append")
    p_dlna.set_defaults(func=cmd_dlna)

    p_remux = sub.add_parser("remux", help="把 part-*.ts 拼成 mp4")
    p_remux.add_argument("dir")
    p_remux.add_argument("-o", "--output", help="输出文件名（默认 recording.mp4）")
    p_remux.add_argument("--ffmpeg", default="ffmpeg")
    p_remux.set_defaults(func=cmd_remux)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
