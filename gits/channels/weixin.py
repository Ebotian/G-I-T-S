"""微信通道：iLink Bot 协议。

协议细节来自官方 npm 包 `@tencent-weixin/openclaw-weixin`（v2.4.10）的源码，
不是猜的：

    登录    POST ilink/bot/get_bot_qrcode?bot_type=3   body {local_token_list}
            GET  ilink/bot/get_qrcode_status?qrcode=…   （服务端 hold 到 35s）
    收消息  POST ilink/bot/getupdates                   body {get_updates_buf}
    发消息  POST ilink/bot/sendmessage                  body {msg:{…}}
    正在输入 POST ilink/bot/sendtyping

两个必须记住的细节：

  **context_token**：每条收到的消息都带，回复时必须原样带回，否则消息挂不到会话上。
  **X-WECHAT-UIN**：base64(随机 uint32 的十进制字符串)，每次请求都要变。

长轮询会 hold 35 秒，所以它跑在**后台线程**里，把消息塞进队列；
主循环（单写者）只从队列取——SQLite 写权限不跨线程。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
APP_ID = "bot"
CLIENT_VERSION = (2 << 16) | (4 << 8) | 10      # 2.4.10 → 0x0002040A

MSG_USER, MSG_BOT = 1, 2
STATE_FINISH = 2
ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5


@dataclass
class Account:
    token: str
    base_url: str = DEFAULT_BASE
    ilink_bot_id: str = ""
    user_id: str = ""

    @staticmethod
    def load(path: str | Path) -> "Account":
        return Account(**json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1),
                     encoding="utf-8")
        os.chmod(p, 0o600)          # 里面有 bot_token，等于半个账号


def _uin() -> str:
    """X-WECHAT-UIN：随机 uint32 → 十进制字符串 → base64。每次请求都变。"""
    return base64.b64encode(str(int.from_bytes(os.urandom(4), "big")).encode()).decode()


class WeixinClient:
    def __init__(self, account: Account | None = None, *, timeout: float = 20.0) -> None:
        self.account = account
        self.timeout = timeout

    # ── 底层 ──
    def _headers(self, auth: bool = True) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _uin(),
            "iLink-App-Id": APP_ID,
            "iLink-App-ClientVersion": str(CLIENT_VERSION),
        }
        if auth and self.account and self.account.token:
            h["Authorization"] = f"Bearer {self.account.token}"
        return h

    def _request(self, endpoint: str, *, body: dict | None = None,
                 method: str = "POST", timeout: float | None = None,
                 auth: bool = True) -> dict:
        base = (self.account.base_url if self.account else DEFAULT_BASE).rstrip("/")
        url = f"{base}/{endpoint}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers(auth))
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
        return json.loads(raw)

    # ── 登录 ──
    def get_qrcode(self) -> dict:
        return self._request(f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}",
                             body={"local_token_list": []}, auth=False)

    def poll_qr(self, qrcode: str, *, verify_code: str | None = None,
                timeout: float = 40.0) -> dict:
        ep = f"ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode)}"
        if verify_code:
            ep += f"&verify_code={urllib.parse.quote(verify_code)}"
        try:
            return self._request(ep, method="GET", timeout=timeout, auth=False)
        except (TimeoutError, OSError):
            return {"status": "wait"}

    # ── 收发 ──
    def get_updates(self, buf: str = "", *, timeout: float = 40.0) -> dict:
        return self._request("ilink/bot/getupdates",
                             body={"get_updates_buf": buf}, timeout=timeout)

    def send_text(self, to: str, text: str, context_token: str | None,
                  client_id: str | None = None) -> dict:
        msg = {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": client_id or f"gits:{int(time.time() * 1000)}",
            "message_type": MSG_BOT,
            "message_state": STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        return self._request("ilink/bot/sendmessage", body={"msg": msg})

    def get_config(self) -> dict:
        return self._request("ilink/bot/getconfig", body={})

    def send_typing(self, user_id: str, ticket: str, status: int = 1) -> dict:
        return self._request("ilink/bot/sendtyping",
                             body={"ilink_user_id": user_id,
                                   "typing_ticket": ticket, "status": status})


def text_of(msg: dict) -> str:
    """把一条 WeixinMessage 摊成一行文字。语音条自带转写，直接用。"""
    for item in msg.get("item_list") or []:
        t = item.get("type")
        if t == ITEM_TEXT:
            return (item.get("text_item") or {}).get("text", "")
        if t == ITEM_VOICE:
            v = item.get("voice_item") or {}
            return v.get("text") or "[语音]"
        if t == ITEM_IMAGE:
            return "[图片]"
        if t == ITEM_FILE:
            return "[文件] " + str((item.get("file_item") or {}).get("file_name", ""))
        if t == ITEM_VIDEO:
            return "[视频]"
    return ""


class WeixinChannel:
    """给 shell 用的通道。长轮询在后台线程，主循环只从队列取。"""

    name = "weixin"

    def __init__(self, account: Account, *, poll_timeout: float = 40.0) -> None:
        self.account = account
        self.client = WeixinClient(account)
        self.inbox: queue.Queue[dict] = queue.Queue()
        self._buf = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_timeout = poll_timeout

    # ── 后台轮询 ──
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="wx-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                resp = self.client.get_updates(self._buf, timeout=self._poll_timeout)
            except Exception:
                time.sleep(3)                     # 网络抖一下，别把线程打死
                continue
            if resp.get("errcode") == -14:        # 会话超时：清游标重来
                self._buf = ""
                time.sleep(1)
                continue
            buf = resp.get("get_updates_buf")
            if buf:
                self._buf = buf
            for msg in resp.get("msgs") or []:
                if msg.get("message_type") != MSG_USER:
                    continue
                text = text_of(msg)
                if not text:
                    continue
                self.inbox.put({
                    "text": text,
                    "ts": int(msg.get("create_time_ms") or time.time() * 1000),
                    "ext_id": str(msg.get("message_id") or msg.get("seq") or ""),
                    "from": msg.get("from_user_id", ""),
                    "context_token": msg.get("context_token", ""),
                })

    # ── Channel 协议 ──
    def deliver(self, row: dict) -> tuple[str, str]:
        meta = {}
        try:
            meta = json.loads(row.get("meta") or "{}")
        except json.JSONDecodeError:
            pass
        to = meta.get("to")
        if not to:
            return "failed", "no-target"          # 没有收件人就不发，不猜
        try:
            resp = self.client.send_text(to, row["text"], meta.get("context_token"))
        except Exception as exc:
            return "failed", f"err:{type(exc).__name__}"
        if resp.get("ret") not in (0, None):
            return "failed", f"ret:{resp.get('ret')}"
        return "sent", str(resp.get("message_id", ""))

    def poll(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out
