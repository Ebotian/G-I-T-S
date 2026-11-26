"""外部预测：**权威给的，不是我们推的。**

和时间源/空间源放一起，因为它属于同一类东西——外面的世界告诉我们的事。
但它在数据分类上不是"读数"，是**记录**：

    读数   会过期（天气一小时后就变了）
    记录   有效期在将来（"明天有雨"明天之前一直成立）

所以它不进 B 段，进 C 段，而且**只在窗口内出现**——和生日那条通道一模一样
（facts.due 的 lead_days）。

另一类预测是自建的：从时空规律里学"他接下来会不会说话、会去哪"。
那个在 world/coupling.py，两者的区别不是实现，是**证据的来源**：

    外部预测   有人替我们看过，可能是错的，但可以核对
    自建预测   我们自己的估计，永远带不确定性，不能说成事实
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from gits.core.db import DB, Writer

#: 提前几天说。和 facts 的 lead_days 同一个意思。
LEAD_DAYS = 1
#: 温差超过这么多度才值得提
TEMP_SWING = 8.0
#: 这些词出现就要提
NOTABLE = ("雨", "雪", "雷", "雹", "雾", "霾", "沙", "尘", "风")


def fetch(adcode: str, *, key: str, timeout: float = 8.0) -> dict[str, Any]:
    """四天预报。取不到就抛，由调用方决定怎么办。"""
    q = urllib.parse.urlencode({"key": key, "city": adcode, "extensions": "all"})
    with urllib.request.urlopen(
            f"https://restapi.amap.com/v3/weather/weatherInfo?{q}",
            timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    if str(body.get("status")) != "1":
        raise RuntimeError(f"高德预报: {body.get('info')}")
    fs = body.get("forecasts") or []
    if not fs:
        raise RuntimeError(f"高德预报没有数据：adcode={adcode}")
    f = fs[0]
    return {
        "kind": "forecast",
        "adcode": str(f.get("adcode") or adcode),
        "city": str(f.get("city") or ""),
        "reporttime": str(f.get("reporttime") or ""),
        "casts": [
            {
                "date": str(c.get("date") or ""),
                "dayweather": str(c.get("dayweather") or ""),
                "nightweather": str(c.get("nightweather") or ""),
                "daytemp": _num(c.get("daytemp_float") or c.get("daytemp")),
                "nighttemp": _num(c.get("nighttemp_float") or c.get("nighttemp")),
                "daywind": str(c.get("daywind") or ""),
                "daypower": str(c.get("daypower") or ""),
            }
            for c in (f.get("casts") or [])
        ],
    }


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def record(db: DB, fc: dict[str, Any], *, now: int) -> tuple[int, bool]:
    """落成事件。幂等键带报告时间——同一份预报不会落两次。"""
    return Writer(db).append_event(
        source="amap", kind="reading",
        ext_id=f"amap:forecast:{fc.get('adcode')}:{fc.get('reporttime')}",
        ts=now, recv_ts=now,
        payload=json.dumps(fc, ensure_ascii=False).encode())


def latest(db: DB) -> dict[str, Any] | None:
    for row in db.q("SELECT payload FROM events WHERE kind='reading'"
                    " ORDER BY id DESC LIMIT 100"):
        try:
            r = json.loads((row["payload"] or b"{}").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if r.get("kind") == "forecast":
            return r
    return None


def _date(ts: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")


def _notable(cast: dict[str, Any]) -> bool:
    text = f"{cast.get('dayweather', '')}{cast.get('nightweather', '')}"
    if any(w in text for w in NOTABLE):
        return True
    hi, lo = cast.get("daytemp"), cast.get("nighttemp")
    return bool(hi is not None and lo is not None and abs(hi - lo) >= TEMP_SWING * 1.5)


def due(db: DB, now: int, *, lead_days: int = LEAD_DAYS
        ) -> list[tuple[dict[str, Any], int]]:
    """窗口内的预报：[(cast, 还有几天)]。**平时返回空。**

    只说"和平时不一样"的天气。否则每天都是"明天多云"，那就是噪音不是信息。
    """
    fc = latest(db)
    if not fc:
        return []
    today = _date(now)
    out = []
    for cast in fc.get("casts") or []:
        d = cast.get("date") or ""
        if len(d) < 10:
            continue
        left = _days_between(today, d)
        if left < 0 or left > lead_days:
            continue
        if not _notable(cast):
            continue
        out.append((cast, left))
    out.sort(key=lambda x: x[1])
    return out


def _days_between(a: str, b: str) -> int:
    import datetime as dt

    try:
        da = dt.date.fromisoformat(a)
        db_ = dt.date.fromisoformat(b)
    except ValueError:
        return -999
    return (db_ - da).days


def lines(db: DB, now: int) -> tuple[str, ...]:
    """C 段里那几行。**只说天气，不说建议。**"""
    out = []
    for cast, left in due(db, now):
        when = "今天" if left == 0 else ("明天" if left == 1 else f"{left} 天后")
        day = (cast.get("dayweather") or "").strip()
        night = (cast.get("nightweather") or "").strip()
        hi, lo = cast.get("daytemp"), cast.get("nighttemp")
        what = day if day == night or not night else f"白天{day}，夜里{night}"
        temp = f"，{lo:.0f} 到 {hi:.0f} 度" if hi is not None and lo is not None else ""
        out.append(f"{when}{what}{temp}。")
    return tuple(out)
