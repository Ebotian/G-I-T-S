#!/usr/bin/env bash
# 隔离验收：实例读不到设计仓库，也读不到彼此的私有目录。
# 任何一条失败返回非 0。
#
# 为什么要有这个脚本：一层静默失效的防线比没有防线更危险——你会以为它在。
set -uo pipefail

SRC="${GITS_SRC:-/srv/gits}"
DATA="${GITS_DATA:-/var/lib/gits}"
ACTORS="${GITS_ACTORS:-her peer1 peer2}"
MAIN="${GITS_MAIN:-her}"
fail=0

ok()  { echo "  ✓ $*"; }
bad() { echo "  ✗ $*"; fail=1; }

echo "隔离验收（$(hostname)）："

# ── 1. 读不到设计仓库 ──
for a in $ACTORS; do
  if sudo -u "$a" test -e "$SRC/docs/DESIGN.md" 2>/dev/null; then
    bad "$a 读得到 $SRC/docs/DESIGN.md"
  else
    ok "$a 读不到设计文档"
  fi
done

# ── 2. 读不到别人的私有目录 ──
for a in $ACTORS; do
  for b in $ACTORS; do
    [ "$a" = "$b" ] && continue
    if sudo -u "$a" test -r "$DATA/$b" 2>/dev/null; then
      bad "$a 能进 $b 的私有目录"
    fi
  done
done
ok "实例之间互相进不去私有目录"

# ── 3. 同样读得到共享库 ──
for a in $ACTORS; do
  if sudo -u "$a" test -r "$DATA/shared" 2>/dev/null; then
    ok "$a 读得到共享目录"
  else
    bad "$a 读不到 $DATA/shared —— 它会跑不起来"
  fi
done

# ── 4. systemd 那一层 ──
for u in gits-ghost gits-shell; do
  if ! systemctl cat "$u.service" >/dev/null 2>&1; then
    continue
  fi
  if systemctl cat "$u.service" | grep -q '^InaccessiblePaths=/srv/gits'; then
    ok "$u 有 InaccessiblePaths"
  else
    bad "$u 没有 InaccessiblePaths"
  fi
  usr=$(systemctl show "$u.service" -p User --value)
  if [ -n "$usr" ] && [ "$usr" != "root" ]; then
    ok "$u 以 $usr 运行（不是 root）"
  else
    bad "$u 以 root 运行"
  fi
done

exit $fail
