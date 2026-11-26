"""离线对比：时间加权的层次 Beta  vs  GBDT。

**这是实验脚本，不是测试。** 它的结论只能是"在我们现在的数据形状上"，
换一批数据就要重跑。

口径：
  - 滚动起点：按天切，训练 [0, d)，测试第 d 天
  - 指标：Brier（概率的均方误差），越小越好
  - 样本权重：每条定位代表的那段时长（采样不是均匀的）
  - 基线：层次 Beta（place x hour，向全局收缩），就是 coupling.rates 那一套
  - 候选：lightgbm，二分类，特征 = 小时 sin/cos、周末、距上次消息、场所

跑法：  .venv/bin/python -m synth.eval_predict
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
import tempfile
from pathlib import Path

import numpy as np

from gits.core.db import DB
from gits.world import places as PL
from synth import track

DAYS = 21
T0 = int(dt.datetime(2026, 10, 1).timestamp() * 1000)
WINDOW_MS = 5 * 60_000
PRIOR_N = 20.0


def _dataset(db: DB, mts: list[int]) -> dict:
    fixes = [(int(r["ts"]), int(r["place_id"]), int(r["dwell_ms"] or 0))
             for r in db.q("SELECT ts, place_id, dwell_ms FROM place_fixes"
                           " WHERE ts>0 ORDER BY ts")]
    places = sorted({p for _t, p, _d in fixes})
    code = {p: i for i, p in enumerate(places)}
    X, y, w, hour, pid, ts = [], [], [], [], [], []
    for t, p, dwell in fixes:
        d = dt.datetime.fromtimestamp(t / 1000)
        i = bisect.bisect_left(mts, t)
        hit = 1.0 if i < len(mts) and (mts[i] - t) <= WINDOW_MS else 0.0
        X.append([math.sin(2 * math.pi * d.hour / 24),
                  math.cos(2 * math.pi * d.hour / 24),
                  1.0 if d.weekday() >= 5 else 0.0,
                  0.0, float(code[p])])          # 第 4 列留给"距上次消息"
        y.append(hit)
        w.append(max(dwell, 1) / 60_000.0)
        hour.append(d.hour)
        pid.append(p)
        ts.append(t)
    # 距上次消息（小时），回填
    prev = None
    for i, t in enumerate(ts):
        j = bisect.bisect_left(mts, t) - 1
        gap = (t - mts[j]) / 3_600_000 if j >= 0 else 24.0
        X[i][3] = math.log1p(min(gap, 48.0))
        prev = gap
    return {"X": np.asarray(X, dtype=float), "y": np.asarray(y),
            "w": np.asarray(w), "hour": hour, "pid": pid, "ts": ts}


WEEKEND = False


def _beta_predict(train: dict, test: dict) -> np.ndarray:
    """层次 Beta：place x hour，向全局收缩。和 coupling.rates 同一套。

    WEEKEND=1 时多分一层周末——**只加一个特征，不换模型**。
    """
    gk, gn = np.zeros(48), np.zeros(48)
    pk, pn = {}, {}
    for i in range(len(train["y"])):
        h, p, y, w = train["hour"][i], train["pid"][i], train["y"][i], train["w"][i]
        h = h + (24 if (WEEKEND and train["X"][i][2] > 0.5) else 0)
        gk[h] += w * y
        gn[h] += w
        pk.setdefault(p, np.zeros(48))[h] += w * y
        pn.setdefault(p, np.zeros(48))[h] += w
    g = (gk + 1) / (gn + 2)
    out = np.zeros(len(test["y"]))
    for i in range(len(test["y"])):
        h, p = test["hour"][i], test["pid"][i]
        h = h + (24 if (WEEKEND and test["X"][i][2] > 0.5) else 0)
        if p in pk:
            out[i] = (pk[p][h] + PRIOR_N * g[h]) / (pn[p][h] + PRIOR_N)
        else:
            out[i] = g[h]
    return out


def _gbdt_predict(train: dict, test: dict) -> np.ndarray:
    import lightgbm as lgb

    ds = lgb.Dataset(train["X"], label=train["y"], weight=train["w"])
    params = {"objective": "binary", "learning_rate": 0.05, "num_leaves": 7,
              "min_data_in_leaf": 20, "feature_fraction": 0.9,
              "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
              "verbose": -1, "seed": 7, "num_threads": 1}
    bst = lgb.train(params, ds, num_boost_round=60)
    return bst.predict(test["X"])


def main() -> None:
    db = DB(Path(tempfile.mkdtemp()) / "g.db")
    # 采样间隔：手机现在实际是 15 分钟一拍（docs/research/space.md §6）。
    # 4 小时心跳那种极端情形样本太少，任何模型都学不动——那条留给单测。
    import os

    import os as _os

    global WEEKEND
    WEEKEND = _os.environ.get("WEEKEND") == "1"
    interact = os.environ.get("INTERACT") == "1"
    fixes, msgs, truth = track.gen_irregular(start_ms=T0, days=DAYS,
                                             heartbeat_ms=15 * 60_000,
                                             interact=interact)
    track.load(db, fixes, msgs)
    PL.learn(db)
    data = _dataset(db, sorted(msgs))
    print(f"场景 {'带交互' if interact else 'place x hour'}"
          f"   基线{'（多一层周末）' if WEEKEND else '（place x hour）'}")
    print(f"样本 {len(data['y'])}  正例 {int(data['y'].sum())} "
          f"（{data['y'].mean()*100:.2f}%）  场所 {len(set(data['pid']))}")

    day0 = T0
    folds = []
    for d in range(DAYS - 6, DAYS):
        cut = day0 + d * 86_400_000
        nxt = cut + 86_400_000
        tr = [i for i, t in enumerate(data["ts"]) if t < cut]
        te = [i for i, t in enumerate(data["ts"]) if cut <= t < nxt]
        if len(tr) < 50 or len(te) < 5:
            continue
        sub = lambda idx: {k: (np.asarray([data[k][i] for i in idx])
                              if k in ("X", "y") else
                              ([data[k][i] for i in idx] if k != "w"
                               else np.asarray([data[k][i] for i in idx])))
                           for k in data}
        a, b = sub(tr), sub(te)
        pb, pg = _beta_predict(a, b), _gbdt_predict(a, b)
        bb = float(np.mean((pb - b["y"]) ** 2))
        bg = float(np.mean((pg - b["y"]) ** 2))
        folds.append((d, len(te), bb, bg))

    print(f"\n{'天':>3} {'测试样本':>8} {'Beta Brier':>12} {'GBDT Brier':>12}")
    for d, n, bb, bg in folds:
        print(f"{d:>3} {n:>8} {bb:>12.5f} {bg:>12.5f}")
    if folds:
        mb = sum(f[2] for f in folds) / len(folds)
        mg = sum(f[3] for f in folds) / len(folds)
        print(f"\n平均   Beta {mb:.5f}   GBDT {mg:.5f}   "
              f"{'GBDT 好' if mg < mb else 'Beta 好'} {abs(mg-mb)/mb*100:.1f}%")


if __name__ == "__main__":
    main()
