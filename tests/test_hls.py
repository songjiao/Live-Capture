"""HLS 解析 / 码率选择 / AES 解密测试。"""

from __future__ import annotations

import os
import unittest

from live_capture import aes
from live_capture.hls import (
    MasterPlaylist,
    MediaPlaylist,
    Segment,
    is_master_playlist,
    parse_attributes,
    parse_master_playlist,
    parse_media_playlist,
    probe,
    select_variant,
)
from live_capture.util import HttpClient
from tests.helpers import HlsFixture, HlsServer

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.42e01e,mp4a.40.2"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,FRAME-RATE=25.000,CODECS="avc1.640028,mp4a.40.2"
hi/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000001
#EXTINF:4.000,
seg100.ts
#EXTINF:4.000,
seg101.ts
#EXT-X-DISCONTINUITY
#EXTINF:4.000,
seg102.ts
#EXT-X-ENDLIST
"""


class ParseTests(unittest.TestCase):
    def test_parse_attributes_with_quotes(self):
        attrs = parse_attributes('BANDWIDTH=1,CODECS="a,b,c",RESOLUTION=1920x1080')
        self.assertEqual(attrs["BANDWIDTH"], "1")
        self.assertEqual(attrs["CODECS"], "a,b,c")
        self.assertEqual(attrs["RESOLUTION"], "1920x1080")

    def test_master(self):
        master = parse_master_playlist(MASTER, "https://cdn.test/a/index.m3u8")
        self.assertEqual(len(master.variants), 2)
        self.assertEqual(master.variants[1].url, "https://cdn.test/a/hi/index.m3u8")
        self.assertEqual(master.variants[1].height, 1080)
        self.assertTrue(is_master_playlist(MASTER))

    def test_select_variant(self):
        master = parse_master_playlist(MASTER, "https://cdn.test/a/index.m3u8")
        self.assertEqual(select_variant(master, "best").resolution, "1920x1080")
        self.assertEqual(select_variant(master, "worst").resolution, "640x360")
        self.assertEqual(select_variant(master, "1080").bandwidth, 6000000)
        self.assertEqual(select_variant(master, "1").resolution, "1920x1080")
        with self.assertRaises(ValueError):
            select_variant(master, "4k")

    def test_media(self):
        playlist = parse_media_playlist(MEDIA, "https://cdn.test/a/media.m3u8")
        self.assertEqual(playlist.media_sequence, 100)
        self.assertEqual(playlist.last_sequence, 102)
        self.assertTrue(playlist.endlist)
        self.assertEqual(playlist.segments[0].url, "https://cdn.test/a/seg100.ts")
        self.assertFalse(playlist.segments[0].discontinuity)
        self.assertTrue(playlist.segments[2].discontinuity)
        self.assertEqual(playlist.segments[0].key.method, "AES-128")
        self.assertEqual(playlist.segments[0].key.uri, "https://cdn.test/a/key.bin")
        # 没写 IV 时，用分片序号当 IV
        playlist_no_iv = parse_media_playlist(MEDIA.replace(",IV=0x00000000000000000000000000000001", ""), "https://x/y.m3u8")
        self.assertEqual(playlist_no_iv.segments[0].iv, (100).to_bytes(16, "big"))


class AesTests(unittest.TestCase):
    def test_fips197_vector(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(aes.decrypt_cbc(cipher, key, bytes(16)), plain)

    def test_against_cryptography(self):
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            self.skipTest("cryptography 未安装")
        for _ in range(10):
            key, iv = os.urandom(16), os.urandom(16)
            data = os.urandom(777)
            pad = 16 - len(data) % 16
            padded = data + bytes([pad]) * pad
            enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
            cipher = enc.update(padded) + enc.finalize()
            self.assertEqual(aes.decrypt_cbc(cipher, key, iv), data)


class ProbeTests(unittest.TestCase):
    def test_probe_media_playlist(self):
        with HlsServer(HlsFixture(total=4, window=2, interval=999)) as server:
            info = probe(server.url(), HttpClient())
        self.assertEqual(info["kind"], "media")
        self.assertGreaterEqual(info["segments_in_window"], 1)


class SegmentIvTests(unittest.TestCase):
    def test_default_iv_is_sequence(self):
        seg = Segment(sequence=7, url="http://x/7.ts", duration=1.0)
        self.assertEqual(seg.iv, (7).to_bytes(16, "big"))


if __name__ == "__main__":
    unittest.main()
