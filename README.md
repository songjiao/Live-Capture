# wxcap — 微信视频号直播捕获与录制

把手机微信里正在直播的赛事，变成本地可分析的视频文件。

```
┌────────────┐   投屏 (DLNA/UPnP)   ┌──────────────────┐   m3u8    ┌────────────────────┐
│ 手机微信    │ ───────────────────▶ │ wxcap dlna       │ ────────▶ │ wxcap record       │
│ 视频号直播  │  SetAVTransportURI   │ (假装成一台电视)  │  直播地址  │ (HLS/ffmpeg 录制)  │
└────────────┘                      └──────────────────┘           └─────────┬──────────┘
                                                                            │
                                                              part-000.ts / recording.mp4
                                                              index.jsonl / recording.json
```

不改微信客户端、不装根证书、不抓包。用的是微信本来就支持的"投屏到电视"：

1. 程序在局域网里宣告自己是一台 DLNA 电视（MediaRenderer）；
2. 手机微信把直播流地址通过标准 SOAP `SetAVTransportURI` 发给"电视"；
3. 我们只收地址、不解码播放，然后自己按 HLS 分片下载并落盘。

> **验证状态**：解析、录制、断流续录、DLNA 协议栈都有自动化测试（`tests/`），
> 本机 SSDP 发现也已验证可被 M-SEARCH 搜到；但**"真实微信客户端能否搜到并投屏"必须在你的手机上实测**，
> 这是整条链路上唯一无法离线验证的环节。

---

## 快速开始

环境：Python 3.10+，手机与电脑在同一 WiFi；`ffmpeg` 可选（有它才能自动转 mp4）。

```bash
# 在仓库根目录，不需要安装
python3 -m live_capture --help

# 1) 启动"电视"，并在捕获到投屏后自动开始录制
python3 -m live_capture dlna --record recordings/2026-11-17_teamA_vs_teamB

# 2) 手机微信打开视频号直播 → 右上角「…」→ 投屏 → 选择 "wxcap 电视"
#    终端会打印捕获到的直播地址，并自动开始录制

# 3) 比赛结束，Ctrl-C 收尾
```

录完得到：

```
recordings/2026-11-17_teamA_vs_teamB/
├── part-000.ts        # 每 10 分钟一个分片，崩溃/断流最多损失一个分片
├── part-001.ts
├── recording.mp4      # 自动无损转封装（有 ffmpeg 时）
├── recording.json     # 本次录制摘要：时长/分片/断流次数/结束原因
└── index.jsonl        # 分片级时间轴：序号、时长、字节数、断点，供分析对齐
```

## 在另一台机器上部署

换一台和手机同 WiFi 的机器跑（比如笔记本），见 [deploy/README-部署.md](deploy/README-部署.md)，
其中有 Linux/macOS/Windows 步骤、防火墙放行、VPN/多网卡排错和常见故障对照。
快捷方式：

```bash
./deploy/install.sh
./deploy/quickstart.sh "客厅电视" recordings/match01
```

## 两种用法

### A. 投屏自动捕获（推荐）

```bash
python3 -m live_capture dlna --record recordings/match01
```

参数：

| 参数 | 说明 |
| --- | --- |
| `--name` | 投屏列表里显示的名字，默认 `wxcap 电视` |
| `--url-file` | 捕获到的地址写到哪个文件，默认 `captures/current_url.txt` |
| `--record DIR` | 捕获到地址后自动录制到 DIR |
| `--once` | 只捕获一次地址就退出（配合手动录制） |
| `--http-port` | 设备描述的 HTTP 端口，0=随机 |
| `--ssdp-port` | SSDP 监听端口，默认 1900；仅测试用，改了手机就搜不到 |
| `--lan-ip` | 手动指定本机局域网 IP（多网卡 / 开 VPN 时用） |

### B. 已有地址，手动录制

```bash
# 探测码流（清晰度、帧率、分片窗口）
python3 -m live_capture probe "https://pull-l1.wxlivecdn.com/....m3u8?..."

# 录制
python3 -m live_capture record "https://....m3u8?..." -o recordings/test

# 等 dlna 写出地址再录（适合先开 dlna、后点投屏）
python3 -m live_capture record --url-file captures/current_url.txt --wait 600
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--quality` | `best` | `best` / `worst` / `1080` / `720` / 码流序号 |
| `--start` | `window` | 从当前窗口最早的分片开始；`edge`=最新分片 |
| `--backend` | `auto` | `hls`（纯 Python）/ `ffmpeg` / `auto` |
| `--segment-seconds` | `600` | 单个 part 的时长上限 |
| `--max-duration` | `0` | 最长录制秒数，0=不限（可设 `7200` 防挂机） |
| `--max-stall` | `120` | 多少秒没有新分片判定为断流 |
| `--refresh-wait` | `60` | 地址失效后等重新投屏的秒数 |
| `-H 'Referer: ...'` | 无 | 额外请求头（部分 CDN 需要） |

### 其它命令

```bash
# 把 part-*.ts 拼成 mp4（录制时没开 ffmpeg 的话）
python3 -m live_capture remux recordings/match01

# 安装成命令行工具（可选）
python3 -m pip install -e .
wxcap dlna --record recordings/match01
```

## 断流 / 地址过期怎么办

视频号直播地址带时效签名，长场次（90–120 分钟）中途可能失效。本工具的处理：

1. 播放列表返回 401/403/404 时，自动重新读取地址文件；
2. 默认等待 **60 秒**（`--max-stall` 之外单独控制）让你**再投屏一次**；
3. 拿到新地址后，如果分片序号能接上就**直接续录**（不重复、不丢）；接不上则新起一个 part，并在 `index.jsonl` 里留下记录；
4. 全程不会因为单个分片失败而终止。

所以比赛途中如果画面卡住、终端提示"地址已失效"，在手机上重新点一次投屏即可，录制不会中断。

## 已知限制

- **只支持直播，不支持回放/点播**（回放是另一套加密和接口）。
- **iOS 微信可能搜不到设备**：iOS 投屏走 AirPlay，本工具只实现 DLNA。建议用 Android 手机投屏，或换用支持 AirPlay 的现成工具。
- **不是所有直播间都能投屏**：付费直播可能禁止录屏/投屏；微信版本更新也可能调整入口。
- **加密音轨**：个别场次的音轨是加密的，可能出现"有画面无声音"，需要单独处理。
- **单机一路最稳**：多个实例同时监听 1900 会互相抢应答，多路请用多台机器。
- 画质取决于直播间本身：视频号赛事直播普遍是 1080p30，高帧率看主播推流设置。

## 合规与风险（请务必读）

- 微信/腾讯的用户协议**禁止**使用第三方工具抓取、复制客户端与服务器之间的数据；
- 体育赛事画面通常有独立版权（腾讯系 NBA/CBA/英超等监测最严），**录下来自己看**属于个人学习研究的灰色空间，**公开传播或商用几乎必然侵权**；
- 建议：不要用主号长时间挂机录制；不要传播录制内容；仅用于内部技术分析。

详见 [可行性报告](docs/可行性报告.md)。

## 目录结构

```
live_capture/
├── cli.py        # 命令行
├── dlna.py       # 伪 DLNA 电视：SSDP + UPnP + SOAP，捕获 m3u8
├── hls.py        # m3u8 解析、码流选择、分片下载
├── recorder.py   # 录制器（HLS 精确后端 / ffmpeg 通用后端）
├── aes.py        # 纯 Python AES-128-CBC（HLS 加密分片）
└── util.py       # HTTP / 日志 / 原子写
tests/            # 18 个测试：解析、AES、滑动窗口直播、断档、403 续录、ffmpeg 端到端、DLNA SOAP
```

运行测试：

```bash
python3 -m unittest discover -s tests -t . -v
```

## 免责声明

本项目仅供技术研究与个人学习使用。请遵守法律法规与平台协议，勿用于传播、转售或任何商业用途。
