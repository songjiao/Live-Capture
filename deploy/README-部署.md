# 在北京机器上部署与试跑

## 为什么必须在"手机旁边"的机器上跑

DLNA 是**局域网**协议：程序靠 SSDP 组播（`239.255.255.250:1900`）在同一个二层网络里宣告自己，
手机也只在同一局域网内搜索设备。湖南服务器和北京手机不在一个网络，所以：

- 湖南那台机器上**不需要**常驻任何 DLNA 服务（当前也确实没有在跑）；
- 抓流这一步必须在北京、和手机连同一个 WiFi 的机器上执行；
- 录完之后可以把文件再回传到服务器做分析（或者直接在笔记本上分析）。

## 0. 先确认三件事

1. 北京这台机器的系统：Linux / macOS / Windows；
2. 手机是 **Android**（iPhone 走 AirPlay，本工具只实现 DLNA，大概率搜不到设备）；
3. 手机和电脑连的是**同一个路由器**，没有开"访客网络 / AP 隔离"。

> 公司/校园 WiFi 常见 AP 隔离，组播会被拦。最稳的做法：用手机开热点，电脑连热点；
> 或者用家里的路由器。

## 1. 拿到代码

方式 A（能访问 GitHub）：

```bash
git clone git@github.com:songjiao/Live-Capture.git
cd Live-Capture
```

方式 B（从湖南服务器拷压缩包，GitHub 慢时更快）：

```bash
# 在 北京机器 上执行（把 host 换成你的服务器地址）
scp songjiao@<服务器地址>:/home/songjiao/Live-Capture/dist/wxcap-0.1.0.tar.gz .
tar xzf wxcap-0.1.0.tar.gz
cd wxcap-0.1.0
```

## 2. 装依赖

Linux / macOS：

```bash
chmod +x deploy/*.sh
./deploy/install.sh
```

Windows（PowerShell，管理员）：

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
# 或者去 python.org 装 3.10+，ffmpeg 官网下 zip 解压后加入 PATH
```

本项目**没有第三方 Python 依赖**，不用 pip install 也能跑。

## 3. 启动

Linux / macOS：

```bash
./deploy/quickstart.sh "客厅电视" recordings/match01
```

Windows（在解压后的目录里）：

```powershell
py -3 -m live_capture dlna --name "客厅电视" --record recordings\match01 --http-port 18099
```

看到这样的输出就说明服务端起来了：

```
设备名   : 客厅电视
本机 IP: 192.168.1.23（多网卡/开 VPN 选错时用 --lan-ip 指定）
描述地址: http://192.168.1.23:18099/description.xml
```

然后：**手机微信 → 打开视频号直播 → 右上角「…」→ 投屏 → 选择「客厅电视」**。
终端会打印捕获到的 m3u8 地址，并自动开始录制。

## 4. 放行防火墙（搜不到设备的第一大原因）

| 系统 | 操作 |
| --- | --- |
| Ubuntu (ufw) | `sudo ufw allow 1900/udp` 和 `sudo ufw allow 18099/tcp` |
| 其它 Linux | 放行 UDP 1900 入站 + TCP 18099 入站 |
| Windows | 首次运行会弹"是否允许 Python 访问网络"，勾选**专用网络**；漏点了去"Windows 安全中心 → 防火墙 → 允许应用通过防火墙"补上 |
| macOS | 首次运行弹窗选"允许"；系统设置 → 网络 → 防火墙 里确认 Python 已允许 |

另外两个坑：

- **开着 VPN / 代理 TUN 模式**：自动探测的本机 IP 会变成 VPN 地址，手机连不上。先关 VPN，或 `--lan-ip 192.168.x.x` 手动指定真实局域网 IP。
- **多网卡**（有线 + 无线同时连）：同样用 `--lan-ip` 指定手机所在网段的 IP。

## 5. 验证（不依赖手机也能做一半）

```bash
# 1) 本机能不能被 SSDP 搜到（另开一个终端）
python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
s.sendto(b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n',
         ("239.255.255.250", 1900))
print(s.recvfrom(65535)[0].decode()[:200])
PY

# 2) 设备描述能不能访问
curl http://127.0.0.1:18099/description.xml | head -5
```

手机端搜不到设备时，按这个顺序排查：同一 WiFi → 关 VPN → 防火墙 → AP 隔离 → 换 Android 手机。

## 6. 录制过程中

- 比赛结束后 `Ctrl-C`：先写盘，再用 ffmpeg 转 `recording.mp4`；
- 中途地址失效（终端提示"地址已失效，等待重新投屏"）：**在手机上重新点一次投屏**，60 秒内会自动续录；
- 输出：`part-*.ts` + `index.jsonl` + `recording.json` + `recording.mp4`。

## 7. 想把录像带回服务器分析

```bash
# 在北京机器上，传到湖南服务器
rsync -avP recordings/match01/ songjiao@<服务器地址>:/home/songjiao/Live-Capture/recordings/match01/
```

或者只传 mp4：

```bash
scp recordings/match01/recording.mp4 songjiao@<服务器地址>:/home/songjiao/Live-Capture/recordings/
```

## 8. 常见故障对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 投屏列表里没有设备 | 不在同一网段 / 防火墙拦 UDP 1900 / 开了 VPN | 关 VPN、放行 1900/udp、`--lan-ip` 指定 |
| 有设备但点了没反应 | 手机走的是 AirPlay（iPhone） | 换 Android 手机 |
| 终端没有打印地址 | 微信投屏没发 `SetAVTransportURI` | 看终端日志是否有 SOAP 请求；换直播间试 |
| 打印了地址但录制失败 | CDN 需要额外请求头 / 地址很快过期 | `probe` 该地址看返回；加 `-H 'Referer: ...'` |
| 录制无声 | 该场次音轨加密 | 目前未处理，只能保留画面 |
| 录到一半停了 | 地址过期且 60 秒内没重新投屏 | 重投即可，已录部分不受影响 |

## 9. 想让它常驻（可选）

Linux 笔记本可以用 systemd 用户服务，掉线自动重启：

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/wxcap-dlna.service <<EOF
[Unit]
Description=wxcap DLNA capture
[Service]
WorkingDirectory=$HOME/Live-Capture
ExecStart=/usr/bin/python3 -m live_capture dlna --record %h/Live-Capture/recordings/auto --http-port 18099
Restart=on-failure
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now wxcap-dlna
```

注意：这样只负责"待命"，投屏还是要每次手动点。
