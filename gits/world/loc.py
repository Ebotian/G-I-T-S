"""他的位置：**手机主动上报的读数，不是我们追踪出来的。**

三条约束（详见 docs/research/space.md）：

  ① **我们接收，不轮询**
     手机在蜂窝网络下是 CGNAT（实测拿到的是 10.159.144.128），
     服务器不可能主动连它。方向只能是一个：手机 -> 服务器。
     所以"轮询"发生在手机上，服务端只落账。

  ② **坐标换标签才出门**
     坐标进本地，出上下文的是"温州"这种标签。
     每一轮上下文都要发给模型；原值出门等于每天上百次把精确位置发出去。

  ③ **带证据等级**
     gps 15m / wifi 50m / network 30m（自报，偏乐观）/ ip 城市级。
     不同等级进不同层：城市级够算天气，场所层要 gps/wifi。

失联的语义和沉默一致：**没有读数不是"他不见了"，是"我不知道"。**
"""

from __future__ import annotations

import json
import time
from typing import Any

from gits.core.db import DB, Writer

#: 位置多久算"此刻"
FRESH_MS = 4 * 3_600_000
#: 只有这些来源可以进场所层（ip 的误差会让家和公司合并成一个点）
PLACE_SOURCES = frozenset({"gps", "wifi", "network"})


def record(db: DB, *, lat: float, lon: float, acc: float | None = None,
           source: str = "gps", fix_ts: int | None = None,
           now: int | None = None, city: dict[str, Any] | None = None,
           scope: str = "self:her") -> tuple[int, bool]:
    """落成事件。幂等键是**手机给的定位时间**，不是我们收到的时间。

    同一份定位重复上报 = 同一条事件；手机重发不会造成多条轨迹。
    """
    ts = int(fix_ts or now or time.time() * 1000)
    payload = {
        "kind": "loc",
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "acc": float(acc) if acc is not None else None,
        "source": source,
        "fix_ts": ts,
    }
    if city:
        payload.update({k: city[k] for k in ("province", "city", "district",
                                             "adcode") if city.get(k)})
    # 幂等：同一秒同一坐标就是同一条
    ext = f"phone:loc:{ts}:{payload['lat']:.5f},{payload['lon']:.5f}"
    return Writer(db).append_event(
        source="phone", kind="reading", ext_id=ext, ts=ts,
        recv_ts=int(now or ts), payload=json.dumps(payload, ensure_ascii=False).encode(),
        scope=scope)


def latest(db: DB, *, now: int, scope: str = "self:her") -> dict[str, Any] | None:
    """最近一条位置读数。**不按新鲜度筛**，过期与否由调用方判断。"""
    rows = db.q(
        "SELECT payload FROM events WHERE kind='reading' AND scope=?"
        " ORDER BY id DESC LIMIT 50", (scope,))
    for row in rows:
        try:
            r = json.loads((row["payload"] or b"{}").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if r.get("kind") == "loc":
            return r
    return None


def fresh(db: DB, *, now: int, ttl: int = FRESH_MS,
          scope: str = "self:her") -> dict[str, Any] | None:
    r = latest(db, now=now, scope=scope)
    if r is None:
        return None
    ts = int(r.get("fix_ts") or 0)
    if ts and (now - ts) > ttl:
        return None
    return r


def adcode(db: DB, *, now: int, ttl: int = FRESH_MS) -> str | None:
    """由位置推出来的行政区码。**天气跟着他走**，不跟机房走。"""
    r = fresh(db, now=now, ttl=ttl)
    return (r or {}).get("adcode") or None


def line(db: DB, *, now: int) -> str:
    """B 段一行。**说他在哪，不说他怎么想。**

    只到城市级——坐标换标签才出门。
    """
    r = fresh(db, now=now)
    if r is None:
        return ""
    city = (r.get("city") or "").strip()
    if not city:
        return ""
    return f"他这会儿在{city}。"


def reverse(db: DB, lat: float, lon: float, *, key: str) -> dict[str, Any] | None:
    """坐标 -> 城市/区/行政区码。取不到就返回 None，不抛。"""
    import urllib.parse
    import urllib.request

    q = urllib.parse.urlencode({"key": key, "location": f"{lon:.6f},{lat:.6f}",
                                "extensions": "base"})
    try:
        with urllib.request.urlopen(
                f"https://restapi.amap.com/v3/geocode/regeo?{q}", timeout=8) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if str(body.get("status")) != "1":
        return None
    comp = ((body.get("regeocode") or {}).get("addressComponent") or {})
    out = {
        "province": comp.get("province") if isinstance(comp.get("province"), str) else "",
        "city": comp.get("city") if isinstance(comp.get("city"), str) else "",
        "district": comp.get("district") if isinstance(comp.get("district"), str) else "",
        "adcode": comp.get("adcode") if isinstance(comp.get("adcode"), str) else "",
    }
    return {k: v for k, v in out.items() if v}
