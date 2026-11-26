"""迁移文件是**只读的**：已应用的那份不许再改。

这个测试是被一次真实的静默失效逼出来的：

    018 先建了 affect，五分钟后 affect_state 被追加进同一个文件。
    生产库的 user_version 已经是 18，那段 CREATE TABLE 永远不再执行——
    文件改了，库没变。于是 relation() → affect_line() → no such table，
    而单元测试全绿（测试库是从零迁移的，当然有这张表）。

规则很简单：**新结构加新文件**。要改老文件，就得同时改这份清单，
那是一次显式的决定，不是一个手滑。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent.parent / "gits" / "schema"
MANIFEST = SCHEMA / "MANIFEST.sha256"


def _digests() -> dict[str, str]:
    out = {}
    for p in sorted(SCHEMA.glob("[0-9]*_*.sql")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_已应用的迁移没有被改动() -> None:
    got = _digests()
    want = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split()
        want[name] = digest

    changed = [n for n in got if n in want and got[n] != want[n]]
    assert not changed, (
        f"这些迁移文件被改过：{changed}\n"
        "已经应用过的迁移不会重跑，改它对现存库毫无作用，只会让新库和老库长得不一样。\n"
        "正确做法：新加一个 0NN_xxx.sql。确实要修老文件的话，改完再跑\n"
        "    python3 -m gits.cli schema-manifest\n"
        "并把这个决定写进提交说明。")

    added = [n for n in got if n not in want]
    assert not added, (
        f"新迁移 {added} 还没登记进清单。跑：python3 -m gits.cli schema-manifest")


def test_清单里没有多余条目() -> None:
    got = _digests()
    want = {line.split()[1] for line in MANIFEST.read_text(encoding="utf-8").splitlines()
            if line.strip()}
    assert want <= set(got), f"清单里有已不存在的迁移：{sorted(want - set(got))}"
