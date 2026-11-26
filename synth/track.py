"""合成一个人的轨迹与消息，用来检验"时空学习"的机制。

**参数取自真实数据**（docs/research/space.md）：

    锚点        27.8394, 120.8374    真实定位点（温州）
    精度        30 m                 小米 network provider 的 hAcc
    采样        15 分钟              手机上实际的采集间隔
    跳点        40 km                 WiFi <-> 流量 切换造成的假移动量级

它检验的是**实现**：能不能把两个地方分开、会不会被跳点骗、
收缩是不是真的在起作用。它**不检验理论**——合成的规律是我们自己写进去的，
估出来当然对。这一条和 docs/research/memory-data.md 的结论一致。
"""

from __future__ import annotations

import datetime as dt
import math
import random
from typing import Any

#: 真实锚点
HOME = (27.8394, 120.8374)
#: 工作地点：约 5 km 外
WORK = (27.8750, 120.8600)
#: 40 km 外的一个点（网络切换跳点的量级）
FAR = (28.1500, 120.9500)


def _shift(lat: float, lon: float, dn: float, de: float) -> tuple[float, float]:
    return (lat + dn / 111_320.0,
            lon + de / (111_320.0 * math.cos(math.radians(lat))))


def gen(*, start_ms: int, days: int = 7, step_ms: int = 15 * 60_000,
        acc: float = 30.0, seed: int = 7,
        jitter_every: int = 40) -> tuple[list[dict[str, Any]], list[int]]:
    """返回 (定位序列, 消息时间戳序列)。**确定性**：同一个 seed 同一个结果。"""
    rnd = random.Random(seed)
    fixes: list[dict[str, Any]] = []
    msgs: list[int] = []
    n = 0
    t = start_ms
    end = start_ms + days * 86_400_000
    while t < end:
        d = dt.datetime.fromtimestamp(t / 1000)
        weekday = d.weekday() < 5
        hour = d.hour
        if weekday and 9 <= hour < 18:
            base, at_work = WORK, True
        else:
            base, at_work = HOME, False

        lat, lon = base
        # 定位噪声：sigma 取精度的一半，和真实 GPS 报告的量级一致
        sigma = acc / 2
        lat, lon = _shift(lat, lon, rnd.gauss(0, sigma), rnd.gauss(0, sigma))

        # 网络切换：单点跳到 40 公里外，下一条就回来
        if n % jitter_every == jitter_every - 1:
            lat, lon = FAR

        fixes.append({"lat": lat, "lon": lon, "acc": acc,
                      "source": "network", "fix_ts": t})

        # 他发消息：工作日上班时间偶尔，晚上在家经常，其余没有
        if at_work and hour in (10, 11) and rnd.random() < 0.10:
            msgs.append(t + 60_000)
        elif not at_work and 19 <= hour < 23 and rnd.random() < 0.55:
            msgs.append(t + 60_000)
        n += 1
        t += step_ms
    return fixes, msgs


def load(db: Any, fixes: list[dict[str, Any]], msgs: list[int]) -> None:
    """把合成的世界写进库。走和真实数据同一条路径（事件，不是直接写表）。"""
    from gits.core.db import Writer
    from gits.world import loc as L

    with Writer(db).txn() as c:
        for f in fixes:
            c.execute(
                "INSERT INTO events(ts, recv_ts, source, kind, ext_id, payload,"
                " lag, ts_trust, scope) VALUES(?,?,?,?,?,?,?,?,?)",
                (f["fix_ts"], f["fix_ts"], "phone", "reading",
                 f"phone:loc:{f['fix_ts']}:{f['lat']:.5f},{f['lon']:.5f}",
                 __import__("json").dumps(
                     {"kind": "loc", "lat": round(f["lat"], 6),
                      "lon": round(f["lon"], 6), "acc": f["acc"],
                      "source": f["source"], "fix_ts": f["fix_ts"],
                      "city": "温州市", "adcode": "330303"},
                     ensure_ascii=False).encode(),
                 0, 1, "self:her"))
        for m in msgs:
            c.execute(
                "INSERT INTO events(ts, recv_ts, source, kind, ext_id, payload,"
                " lag, ts_trust, scope) VALUES(?,?,?,?,?,?,?,?,?)",
                (m, m, "shell", "msg_in", f"msg:{m}", b"hi", 0, 1, "world"))
