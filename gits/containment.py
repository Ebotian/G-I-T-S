"""隔离自检：实例**不该**读得到设计仓库。

这条不是洁癖。`/srv/gits/docs/` 里写着为什么这么设计、她的边界在哪、哪些是给人看的、
哪些是给她的。一个能读到这些的实例，是一个读着自己说明书的木偶——
她说的话会开始对齐那份文档，而不是对齐经历。

真正拦住的是三层（缺一层都不算拦住）：

  1. 文件权限   `/srv/gits` 0700 root:root，实例用户连目录都进不去
  2. systemd    `InaccessiblePaths=/srv/gits`，即使权限被改松也挡一层
  3. 进程身份   `User=gits`，不是 root

这个模块只做第 4 层：**自检**。它不拦截（拦不住就不要假装拦），
它只在启动时说一句"隔离失效了"——因为一层静默失效的防线
比一层不存在的防线更危险：你会以为它在。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: 设计仓库的位置。实例不该能读。
REPO = Path(os.environ.get("GITS_REPO_DIR", "/srv/gits"))
#: 这些目录的存在本身就说明"能读到设计"
SENSITIVE = ("docs/DESIGN.md", "docs/research", ".git")


@dataclass(frozen=True)
class Containment:
    ok: bool
    uid: int
    user: str
    problems: tuple[str, ...]

    def line(self) -> str:
        if self.ok:
            return f"隔离 ok（{self.user} 读不到 {REPO}）"
        return "隔离失效：" + "；".join(self.problems)


def check() -> Containment:
    """能读到设计文档就算失败。**读不到不该报错**——那是正常情况。"""
    uid = os.getuid()
    try:
        import pwd

        user = pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):                     # pragma: no cover
        user = str(uid)

    problems: list[str] = []
    if uid == 0:
        problems.append("以 root 运行（隔离全部失效）")
    for rel in SENSITIVE:
        p = REPO / rel
        try:
            if p.exists():
                # exists() 能成功本身就说明目录可读——对 0700 的仓库它会直接 False
                problems.append(f"能读到 {p}")
        except PermissionError:
            continue
    return Containment(ok=not problems, uid=uid, user=user,
                       problems=tuple(problems))
