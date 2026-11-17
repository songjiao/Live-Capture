#!/usr/bin/env bash
# 安装运行依赖：Python 3.10+ 和 ffmpeg（可选但强烈建议）
# 支持 Debian/Ubuntu、Fedora/RHEL、Arch、macOS(Homebrew)
set -euo pipefail

echo "== 检测系统 =="
if command -v apt-get >/dev/null 2>&1; then
  echo "使用 apt 安装 python3 + ffmpeg"
  sudo apt-get update -qq
  sudo apt-get install -y python3 ffmpeg
elif command -v dnf >/dev/null 2>&1; then
  echo "使用 dnf 安装 python3 + ffmpeg"
  sudo dnf install -y python3 ffmpeg
elif command -v pacman >/dev/null 2>&1; then
  echo "使用 pacman 安装 python + ffmpeg"
  sudo pacman -S --noconfirm python ffmpeg
elif command -v brew >/dev/null 2>&1; then
  echo "使用 Homebrew 安装 python + ffmpeg"
  brew install python ffmpeg
else
  echo "未识别的包管理器，请手动安装：Python 3.10+、ffmpeg"
fi

echo
echo "== 版本 =="
python3 -V || true
ffmpeg -version 2>/dev/null | head -1 || echo "ffmpeg 未安装（不影响录制，只影响自动转 mp4）"

echo
echo "== 下一步 =="
echo "  cd $(cd "$(dirname "$0")/.." && pwd)"
echo "  ./deploy/quickstart.sh \"客厅电视\" recordings/match01"
