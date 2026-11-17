"""HLS(m3u8) 播放列表解析与分片下载。

只实现录制直播真正用得到的那一小部分 HLS：

* master playlist → 选码率（清晰度）
* media playlist → 增量拉取新分片
* ``EXT-X-KEY: METHOD=AES-128`` 解密
* ``EXT-X-DISCONTINUITY`` 断点标记
* ``EXT-X-MAP``（fMP4 的初始化段）

不处理：字幕、备用音轨、``SAMPLE-AES`` 等。
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Union

from .aes import decrypt_cbc
from .util import HttpClient, redact_url

log = logging.getLogger("wxcap.hls")

_ATTR_RE = re.compile(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attributes(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for key, value in _ATTR_RE.findall(text):
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[key.upper()] = value
    return attrs


def _resolve(base: str, uri: str) -> str:
    return urllib.parse.urljoin(base, uri.strip())


@dataclass(frozen=True)
class Key:
    method: str
    uri: str | None = None
    iv: bytes | None = None

    @property
    def encrypted(self) -> bool:
        return self.method.upper() == "AES-128"


@dataclass(frozen=True)
class Segment:
    sequence: int
    url: str
    duration: float
    discontinuity: bool = False
    key: Key | None = None
    init_map: str | None = None

    @property
    def iv(self) -> bytes:
        if self.key and self.key.iv:
            return self.key.iv
        return self.sequence.to_bytes(16, "big")


@dataclass
class MediaPlaylist:
    url: str
    target_duration: float
    media_sequence: int
    segments: list[Segment] = field(default_factory=list)
    endlist: bool = False
    version: int = 3

    @property
    def first_sequence(self) -> int:
        return self.segments[0].sequence if self.segments else self.media_sequence

    @property
    def last_sequence(self) -> int:
        return self.segments[-1].sequence if self.segments else self.media_sequence - 1


@dataclass(frozen=True)
class Variant:
    url: str
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""
    frame_rate: str = ""

    @property
    def height(self) -> int:
        try:
            return int(self.resolution.split("x")[1])
        except (IndexError, ValueError):
            return 0

    def describe(self) -> str:
        bits = []
        if self.resolution:
            bits.append(self.resolution)
        if self.frame_rate:
            bits.append(f"{self.frame_rate}fps")
        if self.bandwidth:
            bits.append(f"{self.bandwidth // 1000}kbps")
        return " ".join(bits) or self.url


@dataclass
class MasterPlaylist:
    url: str
    variants: list[Variant] = field(default_factory=list)


Playlist = Union[MasterPlaylist, MediaPlaylist]


def parse_media_playlist(text: str, url: str) -> MediaPlaylist:
    target_duration = 0.0
    media_sequence = 0
    version = 3
    endlist = False
    segments: list[Segment] = []

    sequence = 0
    pending_duration: float | None = None
    pending_discontinuity = False
    pending_key: Key | None = None
    pending_map: str | None = None
    saw_media_sequence = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1].strip())
            sequence = media_sequence
            saw_media_sequence = True
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = float(line.split(":", 1)[1].strip())
        elif line.startswith("#EXT-X-VERSION:"):
            version = int(line.split(":", 1)[1].strip() or 3)
        elif line.startswith("#EXT-X-KEY:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE").upper()
            if method == "NONE":
                pending_key = None
            else:
                uri = attrs.get("URI")
                iv = None
                if attrs.get("IV"):
                    iv = bytes.fromhex(attrs["IV"].lower().removeprefix("0x").rjust(32, "0"))
                pending_key = Key(method=method, uri=_resolve(url, uri) if uri else None, iv=iv)
        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            if attrs.get("URI"):
                pending_map = _resolve(url, attrs["URI"])
        elif line.startswith("#EXT-X-BYTERANGE:"):
            # 视频号基本不用 byte-range；真遇到就整段下载，忽略范围
            log.debug("忽略 EXT-X-BYTERANGE: %s", line)
        elif line.startswith("#EXTINF:"):
            value = line.split(":", 1)[1].split(",", 1)[0].strip()
            try:
                pending_duration = float(value)
            except ValueError:
                pending_duration = 0.0
        elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            pass
        elif line == "#EXT-X-DISCONTINUITY":
            pending_discontinuity = True
        elif line == "#EXT-X-ENDLIST":
            endlist = True
        elif line.startswith("#"):
            continue
        else:
            if not saw_media_sequence:
                sequence = media_sequence
            segments.append(
                Segment(
                    sequence=sequence,
                    url=_resolve(url, line),
                    duration=pending_duration or 0.0,
                    discontinuity=pending_discontinuity,
                    key=pending_key,
                    init_map=pending_map,
                )
            )
            sequence += 1
            pending_duration = None
            pending_discontinuity = False

    return MediaPlaylist(
        url=url,
        target_duration=target_duration or 4.0,
        media_sequence=media_sequence,
        segments=segments,
        endlist=endlist,
        version=version,
    )


def parse_master_playlist(text: str, url: str) -> MasterPlaylist:
    variants: list[Variant] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attributes(line.split(":", 1)[1])
        elif line.startswith("#"):
            continue
        elif pending is not None:
            variants.append(
                Variant(
                    url=_resolve(url, line),
                    bandwidth=int(pending.get("BANDWIDTH", "0") or 0),
                    resolution=pending.get("RESOLUTION", ""),
                    codecs=pending.get("CODECS", ""),
                    frame_rate=pending.get("FRAME-RATE", ""),
                )
            )
            pending = None
    return MasterPlaylist(url=url, variants=variants)


def is_master_playlist(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def select_variant(master: MasterPlaylist, quality: str = "best") -> Variant:
    """按 ``best`` / ``worst`` / ``1080`` / ``720`` / 序号 选一路码流。"""
    if not master.variants:
        raise ValueError("master playlist 里没有可用的码流")
    variants = master.variants
    key = (quality or "best").lower().strip()
    if key in ("best", "max", "highest"):
        return max(variants, key=lambda v: (v.bandwidth, v.height))
    if key in ("worst", "min", "lowest"):
        return min(variants, key=lambda v: (v.bandwidth, v.height))
    if key.isdigit():
        wanted = int(key)
        # 1) 先当清晰度高度（1080/720）匹配
        by_height = [v for v in variants if v.height == wanted]
        if by_height:
            return max(by_height, key=lambda v: v.bandwidth)
        # 2) 再当序号
        if 0 <= wanted < len(variants):
            return variants[wanted]
    raise ValueError(
        "无法匹配清晰度 %r，可选: %s" % (quality, ", ".join(v.describe() for v in variants))
    )


class HlsClient:
    """拉播放列表、下分片、解 AES-128，带 key 缓存。"""

    def __init__(self, http: HttpClient):
        self.http = http
        self._key_cache: dict[str, bytes] = {}

    def fetch(self, url: str) -> Playlist:
        text = self.http.get_text(url)
        if is_master_playlist(text):
            return parse_master_playlist(text, url)
        return parse_media_playlist(text, url)

    def fetch_media(self, url: str) -> MediaPlaylist:
        playlist = self.fetch(url)
        if isinstance(playlist, MasterPlaylist):
            raise ValueError(f"{redact_url(url)} 是 master playlist，预期 media playlist")
        return playlist

    def _key_bytes(self, uri: str) -> bytes:
        if uri not in self._key_cache:
            key = self.http.get_bytes(uri)
            if len(key) != 16:
                raise ValueError(f"AES-128 密钥长度异常: {len(key)} 字节 ({redact_url(uri)})")
            self._key_cache[uri] = key
        return self._key_cache[uri]

    def download_segment(self, segment: Segment) -> bytes:
        data = self.http.get_bytes(segment.url)
        if segment.key and segment.key.encrypted:
            if not segment.key.uri:
                raise ValueError("AES-128 分片缺少 key URI")
            data = decrypt_cbc(data, self._key_bytes(segment.key.uri), segment.iv)
        return data


def probe(url: str, http: HttpClient, quality: str = "best") -> dict:
    """探测一个直播地址：返回码流列表、目标时长、当前分片窗口等。"""
    client = HlsClient(http)
    playlist = client.fetch(url)
    if isinstance(playlist, MasterPlaylist):
        variants = [
            {
                "index": i,
                "bandwidth": v.bandwidth,
                "resolution": v.resolution,
                "frame_rate": v.frame_rate,
                "codecs": v.codecs,
                "url": redact_url(v.url),
            }
            for i, v in enumerate(playlist.variants)
        ]
        chosen = select_variant(playlist, quality)
        media = client.fetch_media(chosen.url)
        return {
            "kind": "master",
            "variants": variants,
            "selected": {
                "resolution": chosen.resolution,
                "bandwidth": chosen.bandwidth,
                "frame_rate": chosen.frame_rate,
            },
            "target_duration": media.target_duration,
            "segments_in_window": len(media.segments),
            "endlist": media.endlist,
        }
    return {
        "kind": "media",
        "target_duration": playlist.target_duration,
        "segments_in_window": len(playlist.segments),
        "endlist": playlist.endlist,
        "encrypted": bool(playlist.segments and playlist.segments[0].key),
    }
