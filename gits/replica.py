"""可恢复的单向副本。

里程碑：**源 → 副本**，拉取式，断点续传，重复拉取不产生重复。

三条语义（对应第三轮评审的建议）：

  1. **游标只在应用成功之后推进**。中途崩了就从旧游标重来——
     因为导入按 (source, ext_id) 幂等，重来一遍是安全的。
  2. **游标按流分别记**，不是一个大游标。删除流比事件流急，两者不该绑在一起。
  3. **投影不传**。副本自己从日志集重放；这也是"每天的同步就是一次
     免费的重放回归测试"（DESIGN §15.8）真正的落地方式。

单向是刻意的：副本只读。双向写入要等身份、租约、冲突处理都定稿（§17.8.2）。
"""

from __future__ import annotations

from dataclasses import dataclass

from gits.db import DB, LOG_STREAMS
from gits.events import export_logset, import_logset
from gits.project import dump_projection, restore

#: 拉取顺序：删除优先，人格其次，事件最后。
#: 顺序有意义——删掉的东西不该在"还没删"的事件之后才被处理。
PULL_ORDER = ("redactions", "observability", "model_calls", "events")


@dataclass
class PullResult:
    node: str
    applied: dict[str, int]
    cursors: dict[str, int]
    lag_ms: int | None

    def summary(self) -> str:
        got = ", ".join(f"{k}+{v}" for k, v in self.applied.items() if v)
        return (f"{self.node}: {got or '无新增'}"
                f" · 游标 {self.cursors}"
                f" · 落后 {None if self.lag_ms is None else self.lag_ms // 60_000} 分钟")


def _cursor_map(db: DB, node: str) -> dict[str, int]:
    return {r["stream"]: int(r["last_id"])
            for r in db.q("SELECT stream, last_id FROM sync_cursor WHERE node_id=?",
                          (node,))}


def _set_cursor(db: DB, node: str, stream: str, last_id: int) -> None:
    with db.conn:
        db.conn.execute(
            "INSERT INTO sync_cursor(node_id, stream, last_id, last_sync_ok)"
            " VALUES(?,?,?,strftime('%s','now')*1000)"
            " ON CONFLICT(node_id, stream) DO UPDATE SET"
            " last_id=excluded.last_id, last_sync_ok=excluded.last_sync_ok",
            (node, stream, last_id),
        )


def _max_id(db: DB, stream: str) -> int:
    pk = "ts_start" if stream == "observability" else "id"
    return int(db.scalar(f"SELECT coalesce(max({pk}), 0) FROM {stream}") or 0)


def pull(src: DB, dst: DB, node: str, *, limit_rows: int = 500) -> PullResult:
    """从 src 拉到 dst。

    `limit_rows` 让一次拉取有上界——副本可能睡了一周才醒，一次灌十万行
    会把内存和事务撑爆。分多次拉完是调用方的事（反复调用直到 applied 全为 0）。
    """
    cursors = _cursor_map(dst, node)
    applied: dict[str, int] = {}

    for stream in PULL_ORDER:
        after = cursors.get(stream, 0)
        payload = {stream: list(_rows_after(src, stream, after, limit_rows))}
        if not payload[stream]:
            applied[stream] = 0
            continue

        before = _count(dst, stream)
        import_logset(dst, payload)
        applied[stream] = _count(dst, stream) - before

        # ★ 应用成功之后才推进游标。崩在这之前，下次从旧游标重来——
        # 导入是幂等的，重来一遍不会多出行。
        last = max(int(r[_pk(stream)]) for r in payload[stream])
        _set_cursor(dst, node, stream, last)
        cursors[stream] = last

    lag = dst.scalar(
        "SELECT min(last_sync_ok) FROM sync_cursor WHERE node_id=?", (node,))
    lag_ms = None if not lag else max(0, _now() - int(lag))
    return PullResult(node=node, applied=applied, cursors=cursors, lag_ms=lag_ms)


def _pk(stream: str) -> str:
    return "ts_start" if stream == "observability" else "id"


def _rows_after(db: DB, stream: str, after: int, limit: int) -> list[dict]:
    from gits.events import iter_stream
    out = []
    for row in iter_stream(db, stream, after):
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _count(db: DB, stream: str) -> int:
    return int(db.scalar(f"SELECT count(*) FROM {stream}") or 0)


def _now() -> int:
    import time
    return int(time.time() * 1000)


def verify(src: DB, dst: DB) -> tuple[bool, str]:
    """副本重放之后，投影必须和源一致。

    只比**稳定字段**：自增 id 在两边必然不同（副本是按拉取顺序插入的），
    但节点、内容、边的语义必须一样。
    """
    try:
        restore(src)
        restore(dst)
    except Exception as e:                     # 缺模型结果之类
        return False, f"重放失败：{type(e).__name__}: {e}"
    a = dump_projection(src).canonical(stable=True)
    b = dump_projection(dst).canonical(stable=True)
    if a == b:
        return True, "一致"
    # 给出可读的差异提示，而不是一大坨 JSON
    import json
    da, db_ = json.loads(a), json.loads(b)
    diffs = [f"{k}: 源 {len(da[k])} vs 副本 {len(db_[k])}"
             for k in da if len(da[k]) != len(db_[k])]
    return False, "不一致 → " + ("; ".join(diffs) or "内容不同但条数相同")
