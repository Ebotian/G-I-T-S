"""微信通道：只测本地能测的。

联网部分（扫码、长轮询、发消息）不能进单测——它们要真的账号。
所以这里守的是：消息摊平、凭据落盘权限、没有收件人时不瞎发。
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest

from gits.channels.weixin import Account, WeixinChannel, WeixinClient, _uin, text_of


# ── 消息摊平 ────────────────────────────────────────────────
def test_文本消息() -> None:
    assert text_of({"item_list": [{"type": 1, "text_item": {"text": "在吗"}}]}) == "在吗"


def test_语音条用自带转写() -> None:
    """微信语音条自带文字，不需要我们再跑 ASR。"""
    msg = {"item_list": [{"type": 3, "voice_item": {"text": "今天好累"}}]}
    assert text_of(msg) == "今天好累"


def test_没有转写的语音给占位() -> None:
    assert text_of({"item_list": [{"type": 3, "voice_item": {}}]}) == "[语音]"


def test_图片文件视频() -> None:
    assert text_of({"item_list": [{"type": 2, "image_item": {}}]}) == "[图片]"
    assert text_of({"item_list": [{"type": 5, "video_item": {}}]}) == "[视频]"
    msg = {"item_list": [{"type": 4, "file_item": {"file_name": "a.pdf"}}]}
    assert text_of(msg) == "[文件] a.pdf"


def test_空消息返回空串() -> None:
    assert text_of({}) == ""
    assert text_of({"item_list": []}) == ""


# ── 凭据 ────────────────────────────────────────────────────
def test_存账号时权限是_600(tmp_path: Path) -> None:
    """文件里是 bot_token，等于半个账号。"""
    p = tmp_path / "wx.json"
    Account(token="tok", ilink_bot_id="bot1", user_id="u1").save(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert Account.load(p).token == "tok"


def test_X_WECHAT_UIN_是十进制字符串的_base64() -> None:
    u = _uin()
    assert base64.b64decode(u).decode().isdigit()


def test_请求头带鉴权和版本() -> None:
    c = WeixinClient(Account(token="tok"))
    h = c._headers()
    assert h["Authorization"] == "Bearer tok"
    assert h["AuthorizationType"] == "ilink_bot_token"
    assert h["iLink-App-Id"] == "bot"
    assert h["Content-Type"] == "application/json"


def test_未登录时不带_Authorization() -> None:
    assert "Authorization" not in WeixinClient()._headers()


# ── 通道 ────────────────────────────────────────────────────
def test_没有收件人就不发() -> None:
    """不许猜收件人。猜错了就是把话说到别人那儿。"""
    ch = WeixinChannel(Account(token="t"))
    assert ch.deliver({"text": "在吗", "meta": None}) == ("failed", "no-target")
    assert ch.deliver({"text": "在吗", "meta": "{}"}) == ("failed", "no-target")


def test_发送失败如实返回(monkeypatch) -> None:
    ch = WeixinChannel(Account(token="t"))

    def boom(*a, **k):
        raise RuntimeError("网络断了")

    monkeypatch.setattr(ch.client, "send_text", boom)
    status, meta = ch.deliver({"text": "在吗", "meta": '{"to": "u1"}'})
    assert status == "failed" and meta.startswith("err:")


def test_发送成功带_message_id(monkeypatch) -> None:
    ch = WeixinChannel(Account(token="t"))
    monkeypatch.setattr(ch.client, "send_text",
                        lambda to, text, tok: {"ret": 0, "message_id": "m42"})
    assert ch.deliver({"text": "在", "meta": '{"to": "u1"}'}) == ("sent", "m42")


def test_轮询把后台队列倒空() -> None:
    ch = WeixinChannel(Account(token="t"))
    ch.inbox.put({"text": "a"})
    ch.inbox.put({"text": "b"})
    assert [m["text"] for m in ch.poll()] == ["a", "b"]
    assert ch.poll() == []
