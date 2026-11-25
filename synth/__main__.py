"""命令行入口：

    python -m synth build --scenario drift --days 30 --out var/synth/drift
    python -m synth score var/synth/drift
    python -m synth all                       # 五个场景全跑一遍并打分
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from synth.build import build
from synth.schedule import SCENARIOS
from synth.score import score


def cmd_build(a: argparse.Namespace) -> int:
    corpus = None
    if a.corpus:
        from synth.dialogue import load_corpus
        corpus = list(load_corpus(a.corpus))
    b = build(a.out, scenario=a.scenario, days=a.days, seed=a.seed, corpus=corpus)
    print(json.dumps({"db": str(b.db_path), "truth": str(b.truth_path),
                      "events": b.events, "model_calls": b.calls},
                     ensure_ascii=False))
    return 0


def cmd_score(a: argparse.Namespace) -> int:
    out = Path(a.dir)
    print(json.dumps(score(out / "gits.db", out / "truth.json").as_dict(),
                     ensure_ascii=False, indent=1))
    return 0


def cmd_all(a: argparse.Namespace) -> int:
    rows = []
    for sc in SCENARIOS:
        out = Path(a.out) / sc
        b = build(out, scenario=sc, days=a.days, seed=a.seed)
        r = score(b.db_path, b.truth_path).as_dict()
        rows.append(r)
        print(json.dumps(r, ensure_ascii=False))
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "report.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="synth")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--scenario", default="regular", choices=SCENARIOS)
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--out", default="var/synth/run")
    b.add_argument("--corpus", default=None, help="本地 JSONL 语料路径")
    b.set_defaults(fn=cmd_build)

    s = sub.add_parser("score")
    s.add_argument("dir")
    s.set_defaults(fn=cmd_score)

    al = sub.add_parser("all")
    al.add_argument("--days", type=int, default=30)
    al.add_argument("--seed", type=int, default=7)
    al.add_argument("--out", default="var/synth")
    al.set_defaults(fn=cmd_all)
    return int(p.parse_args(argv).fn(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
