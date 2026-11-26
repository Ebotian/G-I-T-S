#!/usr/bin/env bash
# G-I-T-S 在 ECS 上的部署。幂等，可以反复跑。
#
# 两条硬规矩：
#
#   1. **实例读不到设计仓库**（/srv/gits 0700 root:root）
#   2. **一个组，每个实例一个用户**——同组意味着能协作，不同用户意味着
#      "哪个进程写了什么"是确定的，各自的私有目录互相进不去。
#
# 目录：
#   /srv/gits              0700 root:root   仓库 + docs + .git（只有人能看）
#   /srv/gits-runtime      0755 root:root   只有代码的一份拷贝
#   /var/lib/gits/shared   2770 root:gits   共享的库与投递箱（同组可写，setgid 保组）
#   /var/lib/gits/<actor>  0700 <actor>:gits 各自的私有目录（凭据、persona、游标）
set -euo pipefail

REPO="${GITS_REPO:-https://github.com/Ebotian/G-I-T-S.git}"
SRC="${GITS_SRC:-/srv/gits}"
RUN="${GITS_RUN:-/srv/gits-runtime}"
DATA="${GITS_DATA:-/var/lib/gits}"
GROUP="${GITS_GROUP:-gits}"
ACTORS="${GITS_ACTORS:-her peer1 peer2}"
MAIN="${GITS_MAIN:-her}"
EXTRA="${GITS_EXTRA:-dream}"

command -v uv >/dev/null || pip3 install -q uv
git config --global http.version HTTP/1.1 || true
git config --global http.postBuffer 524288000 || true

# ── 1. 一个组，每个实例一个用户 ────────────────────────────
getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"
for a in $ACTORS; do
  if ! id -u "$a" >/dev/null 2>&1; then
    # 主组就是这个共享组：同组可协作，但每个进程的身份是确定的
    useradd --system --gid "$GROUP" --home-dir "$DATA/$a" \
            --shell /usr/sbin/nologin "$a"
    echo "已创建实例用户 $a（组 $GROUP，不能登录）"
  fi
done

# ── 2. 源码（只有人能看） ─────────────────────────────────
# 从 post-receive 钩子里调用时，HEAD 已经是新的，别再 fetch（会绕回自己）。
if [ "${GITS_SKIP_GIT:-0}" = "1" ]; then
  echo "跳过 git（由钩子调用）"
elif [ -d "$SRC/.git" ]; then
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
export UV_INDEX_URL="${UV_INDEX_URL:-http://mirrors.cloud.aliyuncs.com/pypi/simple/}"
( cd "$RUN" && uv sync --extra "$EXTRA" --no-editable --quiet )

# ── 4. 数据：共享 + 各自私有 ──────────────────────────────
mkdir -p "$DATA/shared"
chown root:"$GROUP" "$DATA" "$DATA/shared"
chmod 0750 "$DATA"
chmod 2770 "$DATA/shared"          # setgid：新建文件自动继承组，不会变成孤零零的私有

# 每个实例的私有目录：只有它自己能进。**必须在搬家之前建好**——
# 否则 cp 的目标目录还不存在，set -e 会当场中断整个部署（撞过一次）。
for a in $ACTORS; do
  mkdir -p "$DATA/$a"
  chown "$a:$GROUP" "$DATA/$a"
  chmod 0700 "$DATA/$a"
done

# 旧版把所有东西都堆在 var/ 下，按用途搬过去（幂等：目标已存在就不动）
if [ -d "$SRC/var" ]; then
  for f in "$SRC"/var/*; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    case "$base" in
      gits.db*|outbox.jsonl|inbox.jsonl) dest="$DATA/shared/$base" ;;
      *)                                dest="$DATA/$MAIN/$base" ;;
    esac
    [ -e "$dest" ] || cp -a "$f" "$dest"
  done
fi
chown -R root:"$GROUP" "$DATA/shared"
find "$DATA/shared" -type f -exec chmod 0660 {} + 2>/dev/null || true
find "$DATA/shared" -type d -exec chmod 2770 {} + 2>/dev/null || true
chown -R "$MAIN:$GROUP" "$DATA/$MAIN" 2>/dev/null || true

# 密钥：需要它的实例能读，其他人不行
if [ -f /etc/gits.env ]; then
  chown root:"$GROUP" /etc/gits.env
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
install -m 0644 "$SRC/deploy/gits-ingress.service" /etc/systemd/system/
install -m 0644 "$SRC/deploy/gits-housekeep.timer" /etc/systemd/system/
systemctl daemon-reload

# ── 7. 冒烟 + 自检 ───────────────────────────────────────
echo "已部署 $(git -C "$SRC" log --oneline -1)"
echo "  仓库   $SRC  (0700 root:root，实例不可达)"
echo "  运行   $RUN  ($("$RUN/.venv/bin/python" -V 2>&1))"
echo "  共享   $DATA/shared  (2770 root:$GROUP)"
echo "  实例   $(for a in $ACTORS; do printf '%s ' "$a"; done)"
echo
bash "$SRC/deploy/verify_containment.sh" || true
