"""备份：**日志是唯一真相，所以它得有两份。**

现在只有一个库、一块盘。housekeep 每晚跑的是 `replay --shadow`——那是
**校验**日志（重放能不能重建投影），不是备份：库文件坏了它照样坏。

做法很朴素：`sqlite3.Connection.backup()` 拿一致性快照（不靠拷 wal/shm），
在副本上跑一次 `integrity_check`，再 gzip 存起来。

    **先验再存**    一份坏的备份比没有备份更危险——它会让人以为有退路。
    **完整性不通过就当本次备份失败**，返回 False，让定时任务红。
    **只留最近 N 份**，磁盘不是无限的。

局限说清楚：这是**同机备份**。它挡的是"写坏了 / 误删了 / 迁移把表弄没了"，
挡不了"这块盘没了"。要挡那个，得往另一台机器上放——现在没有第二台。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

#: 默认放哪儿。和系统自带的 /var/backups 分开，免得混在一起被清理。
DEFAULT_DEST = "/var/backups/gits"
#: 留几份。每晚一份 → 两周。周备份再加一层，暂时不做：一份 copy 已经很便宜，
#: 但没有验证过的保留策略等于没保留，先只做能验证的那部分。
DEFAULT_KEEP = 14


def _stamp(now: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(now / 1000).strftime("%Y%m%d-%H%M%S")


def run(db_path: str | Path, *, dest: str | Path = DEFAULT_DEST,
        now: int | None = None, keep: int = DEFAULT_KEEP,
        gzip_it: bool = True) -> dict:
    """做一份备份。**返回结果字典，不抛**——定时任务不该因为备份失败就崩，
    但 `gits status` 要能看到它失败。"""
    now = int(time.time() * 1000) if now is None else now
    src = Path(db_path)
    out_dir = Path(dest)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"gits-{_stamp(now)}.db"
    out = raw.with_suffix(".db.gz") if gzip_it else raw

    info: dict = {"ok": False, "path": str(out), "bytes": 0, "integrity": ""}
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(str(raw))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        # **先验再存。**坏的备份比没有备份更危险——它让人以为有退路。
        integrity, events = _check(raw)
        if integrity != "ok":
            info["integrity"] = integrity
            raw.unlink(missing_ok=True)      # 坏的副本不该留在那里冒充备份
            return info

        if gzip_it:
            with open(raw, "rb") as fin, gzip.open(out, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            raw.unlink()

        data = out.read_bytes()
        info.update(ok=True, bytes=len(data), integrity=integrity,
                    events=events,
                    sha256=hashlib.sha256(data).hexdigest()[:16])
        _prune(out_dir, keep=keep)
    except Exception as exc:                       # 备份失败不该带走进程
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def _check(path: Path) -> tuple[str, int]:
    """验一份副本。单独一个函数，是为了能让测试把"坏"喂进来——
    否则那条分支永远走不到（sqlite3 在 backup 阶段就已经抛了）。"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        events = int(conn.execute("SELECT count(*) FROM events").fetchone()[0])
    finally:
        conn.close()
    return (integrity, events)


def _prune(out_dir: Path, *, keep: int) -> list[str]:
    """只留最近 keep 份。按文件名排——文件名里就是时间。"""
    files = sorted(out_dir.glob("gits-*.db*"), reverse=True)
    gone = []
    for old in files[keep:]:
        try:
            old.unlink()
            gone.append(old.name)
        except OSError:
            pass
    return gone


def latest(dest: str | Path = DEFAULT_DEST) -> str | None:
    files = sorted(Path(dest).glob("gits-*.db*"), reverse=True)
    return files[0].name if files else None


def verify(path: str | Path) -> tuple[bool, str]:
    """验一份备份能不能用。**解压到临时文件再 integrity_check。**"""
    p = Path(path)
    try:
        if p.suffix == ".gz":
            tmp = p.with_suffix("")
            with gzip.open(p, "rb") as fin, open(tmp, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            target = tmp
        else:
            target = p
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            got = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            n = int(conn.execute("SELECT count(*) FROM events").fetchone()[0])
        finally:
            conn.close()
        if p.suffix == ".gz":
            Path(target).unlink(missing_ok=True)
        return (got == "ok", f"integrity={got} events={n}")
    except Exception as exc:
        return (False, f"{type(exc).__name__}: {exc}")
