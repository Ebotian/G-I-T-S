#!/usr/bin/env bash
# G-I-T-S 在 ECS 上的部署。幂等，可以反复跑。
set -euo pipefail

REPO="${GITS_REPO:-https://github.com/Ebotian/G-I-T-S.git}"
HOME_DIR="${GITS_HOME:-/srv/gits}"
EXTRA="${GITS_EXTRA:-dream}"

command -v uv >/dev/null || pip3 install -q uv

if [ -d "$HOME_DIR/.git" ]; then
  git -C "$HOME_DIR" fetch -q origin
  git -C "$HOME_DIR" reset -q --hard origin/main
else
  mkdir -p "$(dirname "$HOME_DIR")"
  git clone -q "$REPO" "$HOME_DIR"
fi

cd "$HOME_DIR"
mkdir -p var

# 阿里云镜像：uv 不读 pip.conf，得单独给
export UV_INDEX_URL="${UV_INDEX_URL:-http://mirrors.cloud.aliyuncs.com/pypi/simple/}"
uv sync --extra "$EXTRA" --quiet

echo "已部署 $(git -C "$HOME_DIR" log --oneline -1)"
echo "  代码   $HOME_DIR"
echo "  解释器 $HOME_DIR/.venv/bin/python ($(.venv/bin/python -V))"
echo "  数据   $HOME_DIR/var"
echo
echo "冒烟："
.venv/bin/python -m gits.cli --db var/gits.db status
