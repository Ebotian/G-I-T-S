"""入口：唯一对公网开的那道门。

四件事各有测试：只认一条路由、签名、重放、报文上限。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from gits.core.db import DB
from gits.io import ingress

SECRET = "test-secret-please-change"


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _sign(ts: int, body: bytes, secret: str = SECRET) -> str:
    return ingress.Verifier(secret).sign(ts, body)


def test_签名对时间窗外的拒绝() -> None:
    v = ingress.Verifier(SECRET)
    now = time.time()
    body = b'{"latitude":27.8,"longitude":120.8}'
    ts = int(now * 1000)
    assert v.check_sig(str(ts), _sign(ts, body), body, now)
    old = ts - (ingress.WINDOW_S + 10) * 1000
    assert not v.check_sig(str(old), _sign(old, body), body, now), "过期签名被接受"


def test_同一组签名只能过一次() -> None:
    """重放窗口：网络重传可以重发，但同一报文不能落两次账。"""
    v = ingress.Verifier(SECRET)
    now = time.time()
    body = b'{"latitude":27.8,"longitude":120.8}'
    ts = int(now * 1000)
    sig = _sign(ts, body)
    assert v.check_sig(str(ts), sig, body, now)
    assert not v.check_sig(str(ts), sig, body, now), "重放被接受"


def test_签名不对就拒() -> None:
    v = ingress.Verifier(SECRET)
    now = time.time()
    ts = int(now * 1000)
    body = b"{}"
    assert not v.check_sig(str(ts), _sign(ts, b"{}", "别的密钥"), body, now)


def test_限流() -> None:
    v = ingress.Verifier(SECRET, rate_per_min=3)
    now = time.time()
    assert all(v.allow("1.2.3.4", now) for _ in range(3))
    assert not v.allow("1.2.3.4", now), "限流没生效"
    assert v.allow("5.6.7.8", now), "限流串了来源"


def test_报文解析只认该认的字段() -> None:
    got = ingress.parse(json.dumps({
        "latitude": 27.8, "longitude": 120.8, "accuracy": 30,
        "provider": "network", "elapsedMs": 5, "phone_ts": 1_800_000_000_000,
        "evil": "../../etc/passwd"}).encode())
    assert got is not None
    assert got["lat"] == 27.8 and got["acc"] == 30
    assert got["fix_ts"] == 1_800_000_000_000
    assert "evil" not in got, "多余字段被带进来了"
    assert ingress.parse(b"not json") is None
    assert ingress.parse(b'{"latitude": "x", "longitude": 1}') is None


def _serve(db: DB, *, rate_per_min: int = ingress.RATE_PER_MIN):
    srv = ingress.ThreadingHTTPServer(("127.0.0.1", 0), ingress.Handler)
    srv.state = {"db_path": str(db.path),
                 "verifier": ingress.Verifier(SECRET, rate_per_min=rate_per_min)}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], "127.0.0.1"


def _post(host: str, port: int, path: str, body: bytes, ts: int, sig: str):
    req = urllib.request.Request(
        f"http://{host}:{port}{path}", data=body, method="POST",
        headers={"X-Ts": str(ts), "X-Sig": sig,
                 "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=5)


def _body(ts: int, **extra) -> bytes:
    d = {"latitude": 27.8394, "longitude": 120.8374, "accuracy": 30,
         "provider": "network", "elapsedMs": 900, "phone_ts": ts}
    d.update(extra)
    return json.dumps(d).encode()


def test_端到端只认一条路由(db) -> None:
    port, host = _serve(db)
    now = int(time.time() * 1000)
    body = _body(now)
    with _post(host, port, "/loc", body, now, _sign(now, body)) as r:
        assert r.status == 204
        assert r.read() == b"", "入口不该回任何内容"

    assert db.scalar("SELECT count(*) FROM events WHERE source='phone'") == 1

    # 别的路径一律 404，而且是**新签名**——不然会被重放窗挡住，测不到路由判断
    for i, path in enumerate(("/", "/admin", "/loc/x", "/loc/")):
        ts = now + 1 + i
        b = _body(ts)
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(host, port, path, b, ts, _sign(ts, b))
        assert e.value.code == 404
        assert e.value.read() == b""
    assert db.scalar("SELECT count(*) FROM events WHERE source='phone'") == 1


def test_报文超长直接拒(db) -> None:
    """**签名合法、JSON 合法，只是太大**——这样才测得到上限本身。"""
    port, host = _serve(db)
    now = int(time.time() * 1000)
    body = _body(now, pad="x" * (ingress.MAX_BODY + 100))
    assert len(body) > ingress.MAX_BODY
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(host, port, "/loc", body, now, _sign(now, body))
    assert e.value.code == 404
    assert db.scalar("SELECT count(*) FROM events") == 0


def test_超过频率就拒(db) -> None:
    port, host = _serve(db, rate_per_min=3)
    base = int(time.time() * 1000)
    for i in range(3):
        ts = base + i
        b = _body(ts)
        with _post(host, port, "/loc", b, ts, _sign(ts, b)) as r:
            assert r.status == 204
    ts = base + 3
    b = _body(ts)
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(host, port, "/loc", b, ts, _sign(ts, b))
    assert e.value.code == 404
    assert db.scalar("SELECT count(*) FROM events WHERE source='phone'") == 3


def test_签名错了不落账(db) -> None:
    port, host = _serve(db)
    now = int(time.time() * 1000)
    body = _body(now)
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(host, port, "/loc", body, now, "0" * 64)
    assert e.value.code == 404
    assert db.scalar("SELECT count(*) FROM events") == 0
