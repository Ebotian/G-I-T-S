"""外面的世界：天气、位置。**读数是事件，不是记忆，也不是实时查询。**

三条设计约束，每条都对应一个具体会坏的地方：

  **① 读数是事件**
     装配上下文时现取天气，重放就会拿到不同的天气——
     "她那天说的话"无法复现，而 events 是唯一真相。
     所以：取到就落成 reading 事件，装配只读日志。

  **② 位置是配置，不是 IP 猜的**
     她跑在 ECS 上。IP 定位给的是**机房所在城市**，不是他所在的城市。
     实测：开发机和 ECS 两台完全不同位置的机器，IP 定位返回同一个城市。
     所以 adcode 必须显式配置；IP 定位只用来**校验配置对不对**，不覆盖它。

  **③ 读数不触发开口**
     墙钟只做参照层，天气同理。
     "下雨了" 进 B 段（她此刻知道的事），**不进 gate 的 reason**。
     否则 "下雨记得带伞" 就成了剧本——而那不是她想起来的，是规则替她想的。

读数会过期，但**不删**：一小时后它不是"此刻"，却仍是"当时的世界"。
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from gits.core.db import DB, Writer

#: 天气多久算"此刻"。高德实况大约每小时更新一次。
FRESH_MS = 3 * 3_600_000
#: 采集间隔（同一小时内不重复取）
FETCH_MS = 55 * 60_000
#: 单次请求超时。取不到就取不到，不能拖住 tick。
TIMEOUT = 8.0

BASE = "https://restapi.amap.com/v3"


def config() -> tuple[str, str]:
    """(密钥, adcode)。没配就抛，不静默降级成"没有天气"。"""
    key = (os.environ.get("GITS_AMAP_KEY") or "").strip()
    adcode = (os.environ.get("GITS_AMAP_ADCODE") or "").strip()
    if not key:
        raise RuntimeError("没配 GITS_AMAP_KEY")
    if not adcode:
        raise RuntimeError("没配 GITS_AMAP_ADCODE（她「在哪」必须显式给，不能靠 IP 猜）")
    return key, adcode


def _get(path: str, params: dict[str, str], *, key: str) -> dict[str, Any]:
    q = urllib.parse.urlencode({**params, "key": key})
    req = urllib.request.Request(f"{BASE}/{path}?{q}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = json.loads(r.read().decode("utf-8"))
    if str(body.get("status")) != "1":
        info = body.get("info")
        code = body.get("infocode")
        raise RuntimeError(f"高德 {path}: {info} ({code})")
    return body


def fetch_weather(adcode: str, *, key: str) -> dict[str, Any]:
    """实况。**只取实况，不取预报**——预报是"明天"，她活在"此刻"。"""
    body = _get("weather/weatherInfo",
                {"city": adcode, "extensions": "base"}, key=key)
    lives = body.get("lives") or []
    if not lives:
        raise RuntimeError(f"高德天气没有实况：adcode={adcode}")
    w = lives[0]
    return {
        "kind": "weather",
        "adcode": str(w.get("adcode") or adcode),
        "city": str(w.get("city") or ""),
        "weather": str(w.get("weather") or ""),
        "temp": _num(w.get("temperature_float") or w.get("temperature")),
        "humidity": _num(w.get("humidity_float") or w.get("humidity")),
        "wind": str(w.get("winddirection") or ""),
        "power": str(w.get("windpower") or ""),
        #: 报告时间。**新鲜度由它算，不由我们的时钟算**——
        #: 高德自己说这条是几点观测的，比我们猜的准。
        "report": str(w.get("reporttime") or ""),
    }


def fetch_ip(*, key: str) -> dict[str, Any]:
    """IP 定位。**只用于校验配置**，绝不覆盖配置。"""
    body = _get("ip", {}, key=key)
    return {
        "kind": "ip",
        "province": str(body.get("province") or ""),
        "city": str(body.get("city") or ""),
        "adcode": str(body.get("adcode") or ""),
    }


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── 落账 ────────────────────────────────────────────────────


def _ext_id(r: dict[str, Any]) -> str:
    """幂等键。同一份观测重复采集 = 同一条事件。"""
    if r.get("kind") == "weather":
        return f"amap:weather:{r.get('adcode')}:{r.get('report')}"
    return f"amap:ip:{r.get('adcode') or '?'}"


def record(db: DB, reading: dict[str, Any], *, now: int) -> tuple[int, bool]:
    """落成事件。返回 (id, created)。同一份观测重复采集 created=False。"""
    return Writer(db).append_event(
        source="amap", kind="reading", ext_id=_ext_id(reading), ts=now,
        recv_ts=now, payload=json.dumps(reading, ensure_ascii=False).encode())


def maybe(db: DB, *, now: int, key: str, adcode: str) -> dict[str, Any] | None:
    """该取就取。**取不到不抛**——没有天气是正常状态，不能拖住 tick。"""
    last = latest(db, kind="weather", now=now)
    if last and not _stale(last, now=now, ttl=FETCH_MS):
        return None
    try:
        r = fetch_weather(adcode, key=key)
    except Exception:
        return None
    _ev, created = record(db, r, now=now)
    return r if created else None


def latest(db: DB, *, kind: str = "weather", now: int) -> dict[str, Any] | None:
    """最近一条读数。**不按新鲜度筛**——过期与否由调用方判断。"""
    rows = db.q("SELECT payload FROM events WHERE kind='reading'"
                " ORDER BY id DESC LIMIT 50")
    for row in rows:
        try:
            r = json.loads((row["payload"] or b"{}").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if r.get("kind") == kind:
            return r
    return None


def _stale(r: dict[str, Any], *, now: int, ttl: int) -> bool:
    ts = _report_ms(r)
    return ts is None or (now - ts) > ttl


def _report_ms(r: dict[str, Any]) -> int | None:
    """把 2026-11-26 14:08:56 解释成**本地墙钟的毫秒**，不是 UTC。

    高德给的是本地时间；我们内部的 now 也是本地墙钟语义，两边一致才对得上。
    """
    import datetime as dt

    s = r.get("report") or ""
    if len(s) < 19:
        return None
    try:
        d = dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return int(d.timestamp() * 1000)


# ── 给 B 段的一行 ───────────────────────────────────────────


def line(db: DB, *, now: int) -> str:
    """此刻外面什么样子。空串 = 不知道，就不说。

    **说外面，不说建议。** "外面在下雨" 是事实；"记得带伞" 是在替他想。
    """
    r = latest(db, now=now)
    if r is None or _stale(r, now=now, ttl=FRESH_MS):
        return ""
    parts = []
    w = (r.get("weather") or "").strip()
    t = r.get("temp")
    if w:
        parts.append(w)
    if isinstance(t, (int, float)):
        parts.append(f"{t:.0f} 度")
    wind = (r.get("wind") or "").strip()
    power = (r.get("power") or "").strip()
    if wind and power and not power.startswith(("≤", "1")):
        parts.append(f"{wind}风 {power} 级")
    if not parts:
        return ""
    city = (r.get("city") or "").strip()
    where = f"{city}外面" if city else "外面"
    return f"{where}：{'，'.join(parts)}。"
