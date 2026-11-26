"""可见域：一个地方决定"谁看得见什么"。

OOP 的封装不是"有个字段标着 private"，是**访问只能通过对象自己的方法**。
放在每个调用点的 `if scope == ...` 一定会漏——我们刚刚漏过一次：
`scope` 字段写了三个月，检索路径里从来没读过（`grep scope gits/recall.py` 是空的），
因为只有一个用户，所以没被发现。

所以：**recall 必须收 actor，且没有默认值**。添麻烦是故意的。
"""

from __future__ import annotations

WORLD = "world"
SELF_PREFIX = "self:"


def self_of(actor: str) -> str:
    return f"{SELF_PREFIX}{actor}"


def visible(actor: str) -> tuple[str, ...]:
    """这个实例能看见的域。

    现在只有两级（自己的 + 公共）。`protected`（只对某个 peer 可见）
    等真有三个实例、而且真的有东西要共享时再加——**没有数据就不要先造结构**。
    """
    return (WORLD, self_of(actor))
