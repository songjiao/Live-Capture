"""微信视频号直播流捕获与录制工具（个人研究用途）。

模块划分：

- :mod:`live_capture.hls`     HLS 播放列表解析、分片下载、AES-128 解密
- :mod:`live_capture.recorder` 录制器（ffmpeg 后端 / 纯 Python HLS 后端）
- :mod:`live_capture.dlna`    伪 DLNA 接收端，从投屏请求里拿到直播流地址
- :mod:`live_capture.cli`     命令行入口
"""

__version__ = "0.1.0"
