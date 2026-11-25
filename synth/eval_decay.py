"""检验遗忘机制：**预测哪条记忆还会被再提起**。

任务（不是"选出答案"）：
    在时刻 T，把 T 之前出现过的所有事实按"还会不会被想起"排序，
    看未来 7 天里真正被提起的那些，是否排得靠前。

指标：AUC（正例排在负例前的能力）+ precision@10，并按"活跃/沉寂"分层——
只看一个总数会骗人，热门近期的总是赢。

用法：
    python -m synth.eval_decay
"""

from __future__ import annotations

import statistics as st
from dataclasses import dataclass

from gits.decay import MODELS, build
from synth.facts import DAY, Facts, generate

HORIZON = 7 * DAY
STEP = 5 * DAY


@dataclass
class Score:
    model: str
    auc: float
    p10: float
    n_eval: int
    auc_active: float
    auc_dormant: float


def _auc(pos: list[float], neg: list[float]) -> float:
    """Mann–Whitney：正例分数高于负例的比例（并列算半个）。"""
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return wins / (len(pos) * len(neg))


def evaluate(fs: Facts, model: str, *, horizon: int = HORIZON,
             step: int = STEP) -> Score:
    times: dict[str, list[int]] = {}
    for m in fs.mentions:
        times.setdefault(m.fact, []).append(m.ts)

    aucs, p10s, actives, dormants = [], [], [], []
    n_eval = 0

    t = fs.start + 10 * DAY
    while t < fs.end - horizon:
        cands = [f for f in fs.facts
                 if f.born < t and times.get(f.id) and min(times[f.id]) < t]
        if len(cands) > 20:
            scored = []
            for f in cands:
                past = [x for x in times[f.id] if x < t]
                if not past:
                    continue
                stt = build(model, f.born, past)
                scored.append((stt.score(t), stt, f.id))
            scored.sort(key=lambda x: -x[0])
            scored = [(s, sid) for s, _st, sid in scored]
            ids = [sid for _s, sid in scored]

            positive = {f.id for f in cands
                        if any(t <= x < t + horizon for x in times[f.id])}
            if positive and len(positive) < len(ids):
                pos = [s for s, sid in scored if sid in positive]
                neg = [s for s, sid in scored if sid not in positive]
                a = _auc(pos, neg)
                if a == a:
                    aucs.append(a)
                    top = set(ids[:10])
                    p10s.append(len(top & positive) / min(10, len(positive)))
                    n_eval += 1

                    # 分层：T 之前 14 天内有提及的算"活跃"
                    recent = {sid for sid in ids
                              if any(t - 14 * DAY <= x < t for x in times[sid])}
                    ap = [s for s, sid in scored if sid in positive and sid in recent]
                    an = [s for s, sid in scored if sid not in positive and sid in recent]
                    dp = [s for s, sid in scored if sid in positive and sid not in recent]
                    dn = [s for s, sid in scored if sid not in positive and sid not in recent]
                    if ap and an:
                        actives.append(_auc(ap, an))
                    if dp and dn:
                        dormants.append(_auc(dp, dn))
        t += step

    def m(xs):
        clean = [x for x in xs if x == x]
        return st.mean(clean) if clean else float("nan")

    return Score(model=model, auc=m(aucs), p10=m(p10s), n_eval=n_eval,
                 auc_active=m(actives), auc_dormant=m(dormants))


def main(days: int = 180, seeds: tuple[int, ...] = (7, 11, 23, 31, 47)) -> None:
    print(f"窗口 {days} 天 · {len(seeds)} 个种子 · 预测未来 7 天是否被提起\n")
    print(f"{'模型':14s} {'AUC':>15s} {'P@10':>15s} {'AUC沉寂':>15s}")
    print("-" * 62)
    for name in MODELS:
        aucs, p10s, dorm = [], [], []
        for seed in seeds:
            s = evaluate(generate(days=days, seed=seed), name)
            aucs.append(s.auc)
            p10s.append(s.p10)
            dorm.append(s.auc_dormant)

        def fmt(xs: list[float]) -> str:
            clean = [x for x in xs if x == x]
            if not clean:
                return "-"
            return f"{st.mean(clean):.3f}±{(max(clean) - min(clean)) / 2:.3f}"

        tag = " (基线)" if name in ("recency", "frequency") else ""
        print(f"{name + tag:16s} {fmt(aucs):>15s} {fmt(p10s):>15s} {fmt(dorm):>15s}")


if __name__ == "__main__":
    main()
