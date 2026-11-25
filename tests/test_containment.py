"""隔离自检本身也要有测试。

它不拦任何东西，所以它的失败方式是"静默通过"——
这正是最该被测的那种代码：一个永远返回 ok 的函数看起来毫无问题。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gits import containment


def _fake_repo(tmp_path: Path, *, with_docs: bool) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    if with_docs:
        (repo / "docs").mkdir()
        (repo / "docs" / "DESIGN.md").write_text("设计文档", encoding="utf-8")
    return repo


def test_读得到设计文档就是失效(tmp_path: Path, monkeypatch) -> None:
    repo = _fake_repo(tmp_path, with_docs=True)
    monkeypatch.setattr(containment, "REPO", repo)
    got = containment.check()
    if os.getuid() == 0:
        pytest.skip("root 下什么都读得到，这条测不了")
    assert not got.ok
    assert any("DESIGN.md" in p for p in got.problems)
    assert "隔离失效" in got.line()


def test_读不到就是正常(tmp_path: Path, monkeypatch) -> None:
    repo = _fake_repo(tmp_path, with_docs=False)
    monkeypatch.setattr(containment, "REPO", repo)
    got = containment.check()
    assert got.ok, got.problems
    assert "ok" in got.line()


def test_root_一定算失效(monkeypatch) -> None:
    monkeypatch.setattr(containment.os, "getuid", lambda: 0)
    monkeypatch.setattr(containment, "REPO", Path("/nonexistent-gits-repo"))
    got = containment.check()
    assert not got.ok
    assert any("root" in p for p in got.problems), \
        "以 root 跑等于全部隔离失效，哪怕读不到文档"
