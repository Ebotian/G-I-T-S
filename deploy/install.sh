#!/usr/bin/env bash
# G-I-T-S 在 ECS 上的部署。幂等，可以反复跑。
#
# 核心的一条：**实例读不到设计仓库**。
#
#   /srv/gits          0700 root:root   仓库 + docs + .git（只有人能看）
#   /srv/gits-runtime  0755 root:root   运行用的一份拷贝：只有代码，没有 docs/.git/tests
#   /var/lib/gits      0750 gits:gits   数据库 / 游标 / 凭据 / persona
#
# 三层拦截（文件权限 + systemd InaccessiblePaths + 非 root 身份），
# 外加 gits/containment.py 的自检——它不拦，只在失效时说一句。
set -euo pipefail

REPO="${GITS_REPO:-https://github.com/Ebotian/G-I-T-S.git}"
SRC="${GITS_SRC:-/srv/gits}"
RUN="${GITS_RUN:-/srv/gits-runtime}"
DATA="${GITS_DATA:-/var/lib/gits}"
USER_NAME="${GITS_USER:-gits}"
EXTRA="${GITS_EXTRA:-dream}"

command -v uv >/dev/null || pip3 install -q uv

# 阿里云 → GitHub 的 HTTP/2 会间歇性 "Error in the HTTP2 framing layer"，
# 表现是 git fetch 随机失败。钉到 HTTP/1.1 之后好一些（但不是万能药，见 ecs.md §4.1）。
git config --global http.version HTTP/1.1 || true
git config --global http.postBuffer 524288000 || true

# ── 1. 实例的身份 ──────────────────────────────────────────
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin \
          --user-group "$USER_NAME"
  echo "已创建用户 $USER_NAME（系统用户，不能登录）"
fi

# ── 2. 源码（只有人能看） ─────────────────────────────────
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch -q origin && git -C "$SRC" reset -q --hard origin/main
else
  mkdir -p "$(dirname "$SRC")"
  git clone -q "$REPO" "$SRC"
fi

# ── 3. 运行目录（只有代码） ───────────────────────────────
mkdir -p "$RUN"
rsync -a --delete \
      --exclude .venv --exclude var --exclude .git \
      --exclude docs --exclude tests --exclude refs --exclude synth \
      "$SRC/gits/" "$RUN/gits/"
cp -f "$SRC/pyproject.toml" "$RUN/"
[ -f "$SRC/uv.lock" ] && cp -f "$SRC/uv.lock" "$RUN/"

# uv 不读 pip.conf，得单独给镜像
export UV_INDEX_URL="${UV_INDEX_URL:-http://mirrors.cloud.aliyuncs.com/pypi/simple/}"
( cd "$RUN" && uv sync --extra "$EXTRA" --no-editable --quiet )

# ── 4. 数据（实例可读写，人也能看） ───────────────────────
mkdir -p "$DATA"
if [ -d "$SRC/var" ]; then
  # 幂等：只在目标还缺文件时搬，跑第二遍不会覆盖掉线上的库
  for f in "$SRC"/var/*; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    [ -e "$DATA/$base" ] || cp -a "$f" "$DATA/$base"
  done
fi
chown -R "$USER_NAME:$USER_NAME" "$DATA"
chmod 0750 "$DATA"

# 密钥：人和实例读得到，其他人不行
if [ -f /etc/gits.env ]; then
  chown root:"$USER_NAME" /etc/gits.env
  chmod 0640 /etc/gits.env
fi

# ── 5. 最后的锁：仓库对实例关死 ───────────────────────────
chown root:root "$SRC"
chmod 0700 "$SRC"
chown -R root:root "$RUN"
chmod -R a-w "$RUN/gits" "$RUN/pyproject.toml" 2>/dev/null || true
chmod 0755 "$RUN"

# ── 6. systemd ───────────────────────────────────────────
install -m 0644 "$SRC/deploy/gits-ghost.service" /etc/systemd/system/
install -m 0644 "$SRC/deploy/gits-shell.service" /etc/systemd/system/
install -m 0644 "$SRC/deploy/gits-housekeep.service" /etc/systemd/system/
install -m 0644 "$SRC/deploy/gits-housekeep.timer" /etc/systemd/system/
systemctl daemon-reload

# ── 7. 冒烟 + 自检 ───────────────────────────────────────
echo "已部署 $(git -C "$SRC" log --oneline -1)"
echo "  仓库   $SRC  (0700 root:root，实例不可达)"
echo "  运行   $RUN  ($("$RUN/.venv/bin/python" -V 2>&1))"
echo "  数据   $DATA ($USER_NAME)"
echo
"$RUN/.venv/bin/python" -m gits.containment 2>/dev/null || \
  "$RUN/.venv/bin/python" -c "from gits.containment import check; print(check().line())"

echo
echo "隔离验收（任何一条失败都是失败）："
if sudo -u "$USER_NAME" test -r "$SRC/docs/DESIGN.md" 2>/dev/null; then
  echo "  ✗ $USER_NAME 能读设计文档"
else
  echo "  ✓ $USER_NAME 读不到 $SRC/docs/DESIGN.md"
fi
if sudo -u "$USER_NAME" test -x "$SRC" 2>/dev/null; then
  echo "  ✗ $USER_NAME 能进 $SRC 目录"
else
  echo "  ✓ $USER_NAME 进不去 $SRC 目录"
fi
if sudo -u "$USER_NAME" test -r "$DATA/gits.db" 2>/dev/null; then
  echo "  ✓ $USER_NAME 读得到自己的库"
else
  echo "  ? 还没有库（首次部署正常）"
fi
