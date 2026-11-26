"""位置入口：只做一件事的 HTTPS 端点。

手机上报走的是**一个只有一条路由的服务**，因为它是唯一对公网开的门。

设计上的四条，每条都对应一个具体的攻击面：

  只认 POST /loc      其他任何路径、任何方法，一律 404，不回任何内容
  HMAC 签名            HMAC-SHA256(secret, ts|body)，时间窗 +-5 分钟
  重放窗口             同一组 (ts, sig) 只接受一次
  请求体 <= 512 B     位置报文就那么点，超出直接拒

**不回任何内容。** 扫到端口也拿不到版本号、错误信息、甚至不告诉你路由是否存在。

它不是"另一个 API"。它是门。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: 时间窗。手机时钟不准也没关系——只要和服务器相差不超过这个。
WINDOW_S = 300
#: 报文上限
MAX_BODY = 512
#: 每个来源 IP 每分钟最多几次
RATE_PER_MIN = 30


class Verifier:
    """签名 + 重放 + 限流。**纯逻辑，好测。**"""

    def __init__(self, secret: str, *, window_s: int = WINDOW_S,
                 rate_per_min: int = RATE_PER_MIN) -> None:
        self.secret = secret.encode()
        self.window = window_s
        self.rate = rate_per_min
        self._seen: dict[str, float] = {}
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def sign(self, ts: int, body: bytes) -> str:
        return hmac.new(self.secret, f"{ts}|".encode() + body,
                        hashlib.sha256).hexdigest()

    def check_sig(self, ts: str, sig: str, body: bytes, now: float) -> bool:
        try:
            t = int(ts)
        except (TypeError, ValueError):
            return False
        if abs(now - t / 1000.0) > self.window:
            return False
        want = self.sign(t, body)
        if not hmac.compare_digest(want, (sig or "").lower()):
            return False
        with self._lock:
            self._gc(now)
            key = f"{ts}:{sig}"
            if key in self._seen:
                return False            # 重放
            self._seen[key] = now
        return True

    def allow(self, ip: str, now: float) -> bool:
        with self._lock:
            hits = [t for t in self._hits.get(ip, []) if now - t < 60]
            if len(hits) >= self.rate:
                self._hits[ip] = hits
                return False
            hits.append(now)
            self._hits[ip] = hits
        return True

    def _gc(self, now: float) -> None:
        if len(self._seen) < 4096:
            return
        cut = now - self.window * 2
        self._seen = {k: v for k, v in self._seen.items() if v >= cut}


def parse(body: bytes) -> dict[str, Any] | None:
    """报文 -> 参数字典。**只认该认的字段。**"""
    try:
        raw = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        lat = float(raw["latitude"])
        lon = float(raw["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    acc = raw.get("accuracy")
    elapsed = raw.get("elapsedMs", raw.get("elapsed_ms"))
    phone_ts = raw.get("phone_ts")
    cached = raw.get("cached")
    return {
        "lat": lat, "lon": lon,
        "acc": float(acc) if isinstance(acc, (int, float)) else None,
        "source": str(raw.get("provider") or "network")[:16],
        "fix_ts": int(phone_ts) if isinstance(phone_ts, (int, float)) else None,
        "elapsed_ms": int(elapsed) if isinstance(elapsed, (int, float)) else None,
        #: 让客户端明说。Termux 那边靠 elapsedMs < 50 推断，app 那边算 age 更准。
        "cached": bool(cached) if isinstance(cached, bool) else None,
    }


def store(db: Any, data: dict[str, Any], *, now: int) -> tuple[int, bool]:
    """落账。和手机那条 SSH 上报走**同一个入口**（world/loc.py）。"""
    from gits.world import loc

    key = os.environ.get("GITS_AMAP_KEY")
    city = None
    if key:
        city = loc.reverse(db, data["lat"], data["lon"], key=key)
    return loc.record(db, lat=data["lat"], lon=data["lon"], acc=data["acc"],
                      source=data["source"], fix_ts=data["fix_ts"], now=now,
                      city=city, elapsed_ms=data["elapsed_ms"],
                      cached=data.get("cached"))


class Handler(BaseHTTPRequestHandler):
    #: 服务器信息不要漏出去
    server_version = " "
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:   # 静默
        pass

    def _no(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._no()

    def do_HEAD(self) -> None:
        self._no()

    def do_PUT(self) -> None:
        self._no()

    def do_DELETE(self) -> None:
        self._no()

    def do_POST(self) -> None:
        st = self.server.state  # type: ignore[attr-defined]
        if self.path != "/loc":
            return self._no()
        now = time.time()
        if not st["verifier"].allow(self.client_address[0], now):
            return self._no()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._no()
        if n <= 0 or n > MAX_BODY:
            return self._no()
        body = self.rfile.read(n)
        if not st["verifier"].check_sig(self.headers.get("X-Ts", ""),
                                        self.headers.get("X-Sig", ""),
                                        body, now):
            return self._no()
        data = parse(body)
        if data is None:
            return self._no()
        # **每个请求一条自己的连接**：sqlite 连接不能跨线程，
        # 而这里是多线程的。一次上报一条连接，代价可以忽略。
        try:
            from gits.core.db import DB

            db = DB(st["db_path"])
            try:
                _ev, created = store(db, data, now=int(now * 1000))
            finally:
                db.close()
        except Exception:
            return self._no()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()
        _ = created

    def do_OPTIONS(self) -> None:
        self._no()


class _Server(ThreadingHTTPServer):
    """TLS 握手在 accept 时做，**失败要能看见**。

    直接 wrap 监听 socket 的话，握手失败只会表现为"连接被关掉"，
    客户端那头是 SSLHandshakeException，服务端一个字都没有——
    两边都在猜。这里把每一处失败都写进日志。
    """

    ctx: object | None = None

    def get_request(self):
        sock, addr = super().get_request()
        if self.ctx is None:
            return sock, addr
        try:
            return self.ctx.wrap_socket(sock, server_side=True), addr  # type: ignore
        except Exception as exc:
            _log(f"TLS 握手失败 from {addr[0]}: {exc!r}")
            try:
                sock.close()
            except OSError:
                pass
            raise

    def handle_error(self, request, client_address) -> None:
        # 握手失败是**预期内**的（有人在扫端口），不该打整段 traceback。
        pass


def _log(msg: str) -> None:
    import sys

    sys.stderr.write("[ingress] " + msg + "\n")
    sys.stderr.flush()


def serve(*, db_path: str, secret: str, port: int,
          cert: str | None = None, key: str | None = None) -> None:
    srv = _Server(("0.0.0.0", port), Handler)
    srv.state = {"db_path": db_path, "verifier": Verifier(secret)}  # type: ignore
    if cert and key:
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        srv.ctx = ctx
    _log(f"监听 :{port}  {'TLS' if srv.ctx else '明文'}  只认 POST /loc")
    srv.serve_forever()
