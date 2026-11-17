#!/usr/bin/env bash
# 一键启动：伪装成电视 + 捕获投屏 + 自动录制
#
# 用法： ./deploy/quickstart.sh [设备名] [输出目录]
# 例：   ./deploy/quickstart.sh "客厅电视" recordings/match01
#
# 可选环境变量：
#   HTTP_PORT=18099   设备描述 HTTP 端口（防火墙要放行这个端口）
#   LAN_IP=192.168.1.23  多网卡/开 VPN 时手动指定本机局域网 IP
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NAME="${1:-wxcap 电视}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${2:-recordings/$STAMP}"
HTTP_PORT="${HTTP_PORT:-18099}"

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null || {
  echo "需要 Python 3.10+，当前版本：$(python3 -V 2>&1 || echo 未安装)"
  exit 1
}

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[提示] 未检测到 ffmpeg：仍可正常录制，只是结束时不会自动转成 mp4。"
fi

cat <<EOF
──────────────────────────────────────────────
设备名   : $NAME
HTTP 端口: $HTTP_PORT（需放行 TCP，防火墙/安全组）
输出目录 : $OUT

下一步：手机微信打开视频号直播 → 右上角「…」→ 投屏
        在设备列表里选择「$NAME」

手机和本机必须连同一个 WiFi / 局域网。
结束录制：Ctrl-C（会先落盘再转 mp4）
──────────────────────────────────────────────
EOF

exec python3 -m live_capture dlna \
  --name "$NAME" \
  --record "$OUT" \
  --http-port "$HTTP_PORT" \
  --max-duration 9000 \
  ${LAN_IP:+--lan-ip "$LAN_IP"}
