"""他这个时段在不在：**细桶自己估，空白退回到小时。**

为什么不是均匀加密：空的地方是**成片**的（02:00-08:00 每天都空）。
均匀切细只是把同一块空白分成十二份，每份都没有数据——那是发明精度。

所以用收缩：

    p(τ) = (k_τ + m · p_hour) / (days + m)

    k_τ     这个 5 分钟格里，有多少天出现过消息
    days    统计窗口里有几天
    p_hour  那个小时的整体估计（粗先验）
    m       先验的等效天数

空桶：k=0 → p = m·p_hour/(days+m) ≈ p_hour。**平滑，不编。**
密桶：k 大 → p 由它自己决定。**真的有分辨率。**

存的用字典不用数组：2016 格里绝大多数是空的，稀疏存更省。
"""

from __future__ import annotations

from gits.core.db import DB

#: 细桶 = 5 分钟
FINE_MS = 5 * 60_000
#: 统计窗口（天）
WINDOW_DAYS = 28
#: 先验的等效天数。越大越保守——20 天意味着"这个格要出现二十天才算有自己的性格"。
PRIOR_DAYS = 20.0


class Snapshot:
    """一次检查只读一次库，之后所有查询都在内存里。

    `marker` 用来判断能不能跨检查复用：有新消息进来就得重算，
    因为在线率本身就是拿消息算的。
    """

    def __init__(self, db: DB, *, now: int) -> None:
        self.marker = int(db.scalar(
            "SELECT coalesce(max(id), 0) FROM events WHERE kind='msg_in'") or 0)
        # **只数他的消息。** 同伴之间的私信在各自域里（self:peerN），
        # 混进来会让"用户活跃度"被别人的对话带动。
        rows = db.q("SELECT ts FROM events WHERE kind='msg_in'"
                    " AND scope='world' ORDER BY ts DESC LIMIT 5000")
        self.days = max(1, min(WINDOW_DAYS,
                        len({int(r["ts"]) // 86_400_000 for r in rows}) or 1))
        self._hour = [0] * 24
        self._fine: dict[int, set[int]] = {}
        for r in rows:
            ts = int(r["ts"])
            import datetime as dt

            d = dt.datetime.fromtimestamp(ts / 1000)
            self._hour[d.hour] += 1
            slot = (d.weekday() * 288 + d.hour * 12 + d.minute // 5)
            self._fine.setdefault(slot, set()).add(ts // 86_400_000)
        self._top = max(self._hour) or 1

    def hour(self, ts: int) -> float:
        """那个小时的整体估计（粗先验）。没有数据给 0.5——不知道，不是五成在。"""
        if not any(self._hour):
            return 0.5
        import datetime as dt

        return self._hour[dt.datetime.fromtimestamp(ts / 1000).hour] / self._top

    def at(self, ts: int) -> float:
        """那一刻他大概在不在。**空格退回小时，密格用自己。**"""
        import datetime as dt

        d = dt.datetime.fromtimestamp(ts / 1000)
        slot = (d.weekday() * 288 + d.hour * 12 + d.minute // 5)
        k = len(self._fine.get(slot, ()))
        prior = self.hour(ts)
        p = (k + PRIOR_DAYS * prior) / (self.days + PRIOR_DAYS)
        return max(0.0, min(1.0, p))


_cache: dict[str, tuple[int, Snapshot]] = {}


def snapshot(db: DB, *, now: int) -> Snapshot:
    """拿一份快照。**有新消息就重算，否则复用。**"""
    key = str(getattr(db, "path", id(db)))
    hit = _cache.get(key)
    if hit is not None:
        marker, snap = hit
        cur = db.scalar(
            "SELECT coalesce(max(id), 0) FROM events WHERE kind='msg_in'")
        if int(cur or 0) == marker:
            return snap
    snap = Snapshot(db, now=now)
    _cache[key] = (snap.marker, snap)
    return snap


def at(db: DB, ts: int, *, now: int, snap: Snapshot | None = None) -> float:
    """外接口：那一刻大概在不在。"""
    s = snap if snap is not None else snapshot(db, now=now)
    return s.at(ts)
