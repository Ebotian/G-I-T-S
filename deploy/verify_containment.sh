#!/usr/bin/env bash
# 隔离验收：实例读不到设计仓库。任何一条失败都返回非 0。
#
# 为什么要有这个脚本：一层静默失效的防线比没有防线更危险——
# 你会以为它在。所以每次部署、每隔一段时间都跑一遍。
set -uo pipefail

SRC="${GITS_SRC:-/srv/gits}"
DATA="${GITS_DATA:-/var/lib/gits}"
USER_NAME="${GITS_USER:-gits}"
fail=0

ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; fail=1; }

echo "隔离验收（$USER_NAME @ $(hostname)）："

if sudo -u "$USER_NAME" test -x "$SRC"; then
  bad "$USER_NAME 能进入 $SRC"
else
  ok "$USER_NAME 进不去 $SRC"
fi

for f in docs/DESIGN.md docs/research .git; do
  if sudo -u "$USER_NAME" test -e "$SRC/$f" 2>/dev/null; then
    bad "$USER_NAME 读得到 $SRC/$f"
  else
    ok "$USER_NAME 读不到 $f"
  fi
done

if sudo -u "$USER_NAME" test -r "$DATA"; then
  ok "$USER_NAME 读得到自己的数据目录"
else
  bad "$USER_NAME 读不到 $DATA —— 它会跑不起来"
fi

# systemd 那一层：就算权限被改松，也应该被 InaccessiblePaths 挡住
if systemctl cat gits-ghost.service 2>/dev/null | grep -q '^InaccessiblePaths=/srv/gits'; then
  ok "gits-ghost 有 InaccessiblePaths"
else
  bad "gits-ghost 没有 InaccessiblePaths"
fi

if systemctl show gits-ghost.service -p User | grep -qv 'User=root'; then
  ok "gits-ghost 不是 root（$(systemctl show gits-ghost.service -p User)）"
else
  bad "gits-ghost 以 root 运行"
fi

exit $fail
