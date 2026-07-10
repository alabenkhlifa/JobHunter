"""Minimal Chrome DevTools Protocol client used by the auto-apply engine.

This module intentionally uses only the Python standard library so the job apply
pilot can run on small Raspberry Pi deployments without adding Playwright or
Selenium. It talks to Chromium started with --remote-debugging-port.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class CDPError(RuntimeError):
    """Raised when a CDP command fails."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("websocket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_frame(sock: socket.socket) -> str:
    hdr = _recv_exact(sock, 2)
    b1, b2 = hdr
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    masked = bool(b2 & 0x80)
    key = _recv_exact(sock, 4) if masked else b""
    data = _recv_exact(sock, length)
    if masked:
        data = bytes(c ^ key[i % 4] for i, c in enumerate(data))
    opcode = b1 & 0x0F
    if opcode == 8:
        raise EOFError("websocket closed")
    if opcode in (9, 10):  # ping/pong
        return _recv_frame(sock)
    return data.decode("utf-8", "replace")


def _send_frame(sock: socket.socket, text: str) -> None:
    data = text.encode("utf-8")
    key = os.urandom(4)
    n = len(data)
    if n < 126:
        hdr = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    masked = bytes(c ^ key[i % 4] for i, c in enumerate(data))
    sock.sendall(hdr + key + masked)


@dataclass(frozen=True)
class CDPTarget:
    id: str
    title: str
    url: str
    type: str
    websocket_url: str


class CDPClient:
    """Small CDP client for one browser page target."""

    def __init__(self, websocket_url: str, timeout: float = 10.0):
        self.websocket_url = websocket_url
        self.timeout = timeout
        u = urllib.parse.urlparse(websocket_url)
        path = u.path + (("?" + u.query) if u.query else "")
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        resp = self.sock.recv(4096)
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise CDPError(f"websocket upgrade failed: {resp[:200]!r}")
        self._next_id = 0

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        payload: dict[str, Any] = {"id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        _send_frame(self.sock, json.dumps(payload))
        while True:
            message = json.loads(_recv_frame(self.sock))
            if message.get("id") == self._next_id:
                if "error" in message:
                    raise CDPError(f"{method} failed: {message['error']}")
                return message

    def evaluate(self, expression: str, *, return_by_value: bool = True) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": return_by_value,
            },
        )["result"]
        if "exceptionDetails" in result:
            raise CDPError(str(result["exceptionDetails"]))
        value = result["result"]
        return value.get("value") if return_by_value else value

    def upload_file(self, selector: str, file_path: str) -> None:
        self.call("DOM.enable")
        root = self.call("DOM.getDocument", {"depth": -1, "pierce": True})["result"]["root"]["nodeId"]
        node = self.call("DOM.querySelector", {"nodeId": root, "selector": selector})["result"].get("nodeId")
        if not node:
            raise CDPError(f"file input not found: {selector}")
        self.call("DOM.setFileInputFiles", {"nodeId": node, "files": [file_path]})

    def screenshot(self, path: str) -> str:
        data = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})["result"]["data"]
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path


def list_targets(host: str = "127.0.0.1", port: int = 9222, timeout: float = 3.0) -> list[CDPTarget]:
    url = f"http://{host}:{port}/json/list"
    targets = json.load(urllib.request.urlopen(url, timeout=timeout))
    result: list[CDPTarget] = []
    for target in targets:
        if "webSocketDebuggerUrl" not in target:
            continue
        result.append(
            CDPTarget(
                id=target.get("id", ""),
                title=target.get("title", ""),
                url=target.get("url", ""),
                type=target.get("type", ""),
                websocket_url=target["webSocketDebuggerUrl"],
            )
        )
    return result


def connect_first_page(host: str = "127.0.0.1", port: int = 9222, timeout: float = 10.0) -> CDPClient:
    pages = [t for t in list_targets(host, port, timeout) if t.type == "page"]
    if not pages:
        raise CDPError("no CDP page targets found")
    return CDPClient(pages[0].websocket_url, timeout=timeout)
