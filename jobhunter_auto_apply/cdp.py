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
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class CDPError(RuntimeError):
    """Raised when a CDP command fails."""


class BrowserDialogError(CDPError):
    """A dialog needs review, or a recognized validation alert was acknowledged."""

    def __init__(self, dialog_type: str, *, validation: bool = False):
        self.dialog_type = dialog_type
        self.validation = validation
        super().__init__("Application validation alert acknowledged; inspect the form before continuing."
                         if validation else
                         "A browser dialog requires review; inspect the desktop before continuing.")


class BrowserTimeoutError(CDPError):
    """A command timed out with its outcome unknown; never resend it automatically."""


TRUSTED_ALERT_ORIGINS = frozenset({"https://careers.clarivate.com"})
_SUCCESS_ALERTS = frozenset({"resume uploaded successfully"})
_VALIDATION_ALERTS = frozenset({"please upload resume", "please enter all required fields"})


def _alert_action(params: dict[str, Any]) -> str | None:
    url = urllib.parse.urlsplit(str(params.get("url") or ""))
    origin = f"{url.scheme}://{url.netloc.lower()}"
    if (params.get("type") != "alert" or url.username or url.password
            or origin not in TRUSTED_ALERT_ORIGINS):
        return None
    message = " ".join(str(params.get("message") or "").split()).rstrip(".").casefold()
    if message in _SUCCESS_ALERTS:
        return "success"
    if message in _VALIDATION_ALERTS:
        return "validation"
    return None


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
        self.frame_path: tuple[str, ...] = ()
        self._page_enabled = False
        self.pending_dialog: dict[str, Any] | None = None
        self.dialog_events: list[dict[str, str]] = []

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.pending_dialog and method != "Page.handleJavaScriptDialog":
            raise BrowserDialogError(str(self.pending_dialog.get("type") or "unknown"))
        # Subscribe before the action that can open an alert. Enabling after a
        # native modal is already open does not reliably expose it in Chromium.
        if not self._page_enabled and method.startswith(("Runtime.", "Input.", "DOM.")):
            self.call("Page.enable")
        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        _send_frame(self.sock, json.dumps(payload))
        deadline = time.monotonic() + self.timeout
        acknowledgements: set[int] = set()
        response = None
        validation = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self.sock.settimeout(remaining)
                message = json.loads(_recv_frame(self.sock))
                if message.get("method") == "Page.javascriptDialogOpening":
                    dialog = message.get("params") or {}
                    action = _alert_action(dialog)
                    # Dialog text is untrusted and may contain personal information;
                    # keep only the classification in diagnostics and exceptions.
                    self.dialog_events.append({"type": str(dialog.get("type") or "unknown"),
                                               "action": action or "review"})
                    self.dialog_events = self.dialog_events[-20:]
                    if action is None:
                        self.pending_dialog = dialog
                        raise BrowserDialogError(str(dialog.get("type") or "unknown"))
                    validation = validation or action == "validation"
                    self._next_id += 1
                    acknowledgements.add(self._next_id)
                    _send_frame(self.sock, json.dumps({"id": self._next_id,
                        "method": "Page.handleJavaScriptDialog", "params": {"accept": True}}))
                    continue
                if message.get("method") == "Page.javascriptDialogClosed":
                    self.pending_dialog = None
                if message.get("id") in acknowledgements:
                    acknowledgements.remove(message["id"])
                    if "error" in message:
                        raise BrowserDialogError("alert")
                elif message.get("id") == request_id:
                    if "error" in message:
                        raise CDPError(f"{method} failed: {message['error']}")
                    response = message
                if response is not None and not acknowledgements:
                    if method == "Page.enable":
                        self._page_enabled = True
                    if method == "Page.handleJavaScriptDialog":
                        self.pending_dialog = None
                    if validation:
                        raise BrowserDialogError("alert", validation=True)
                    return response
        except (TimeoutError, socket.timeout):
            raise BrowserTimeoutError(
                "Browser command timed out; inspect the desktop for a native dialog. "
                "The action was not retried and its outcome must be checked.") from None
        finally:
            self.sock.settimeout(self.timeout)

    def click(self, selector: str) -> None:
        """Click an unobscured top-level control through trusted browser input."""
        if self.frame_path:
            raise CDPError("native control interaction requires the top-level form")
        self.evaluate(f"""(() => {{const e=document.querySelector({json.dumps(selector)});
          if(!e) throw Error('Control unavailable');
          e.scrollIntoView({{block:'center',behavior:'instant'}});}})()""")
        position = self.evaluate(f"""(() => {{const e=document.querySelector({json.dumps(selector)});
          if(!e||e.disabled) throw Error('Control unavailable');
          const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
          const top=document.elementFromPoint(x,y);
          if(!r.width||!r.height||getComputedStyle(e).visibility==='hidden'||
             (top!==e&&!e.contains(top))) throw Error('Control obscured');
          return {{x,y}};}})()""")
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {"type": kind, "button": "left",
                                                  "clickCount": 1, **position})

    def fill(self, selector: str, value: str) -> None:
        self.evaluate(f"""(() => {{const e=document.querySelector({json.dumps(selector)});
          if(!e||e.disabled||e.readOnly||!['INPUT','TEXTAREA'].includes(e.tagName)||
             ['hidden','password','file','checkbox','radio'].includes(e.type)||
             !e.getBoundingClientRect().width) throw Error('Text control unavailable');
          e.focus();e.select();}})()""")
        self.call("Input.insertText", {"text": value})
        self.evaluate("document.activeElement.blur()")

    def select_option(self, selector: str, label: str) -> None:
        info = self.evaluate(f"""(() => {{const e=document.querySelector({json.dumps(selector)});
          if(!e||e.tagName!=='SELECT'||e.disabled||!e.getBoundingClientRect().width)
            throw Error('Select control unavailable');
          const matches=[...e.options].filter(o=>o.text.trim()==={json.dumps(label)}&&!o.disabled);
          if(matches.length!==1) throw Error('Confirmed option unavailable');
          e.focus();return {{current:e.selectedIndex,target:matches[0].index,count:e.options.length}};}})()""")
        if info["current"] == info["target"]:
            return
        self.click(selector)
        # Native select typeahead commits a trusted change on both Wayland and
        # macOS Chromium; arrow keys alone can only open the popup on macOS.
        for char in label:
            self.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": char, "text": char})
            self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": char})
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                 "windowsVirtualKeyCode": 13})
        self.evaluate("document.activeElement.blur()")
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            selected = self.evaluate(f"document.querySelector({json.dumps(selector)})?.selectedOptions[0]?.text.trim()")
            if selected == label:
                return
            time.sleep(.05)
        raise CDPError('Confirmed select option did not persist; inspect the form without repeating the action.')

    def evaluate(self, expression: str, *, return_by_value: bool = True) -> Any:
        if self.frame_path:
            # Scope DOM operations to the inspected, visible same-origin form.
            # Approval checks still compare the exact top-level browser URL.
            expression = """(() => {
              let target = window;
              for (const selector of FRAME_PATH) {
                const frame = target.document.querySelector(selector);
                if (!frame) throw new Error('Inspected application frame disappeared');
                const style = target.getComputedStyle(frame), rect = frame.getBoundingClientRect();
                if (style.display === 'none' || style.visibility === 'hidden' ||
                    rect.width <= 0 || rect.height <= 0 || !frame.contentDocument ||
                    frame.contentWindow.location.origin !== window.location.origin)
                  throw new Error('Inspected application frame is no longer accessible');
                target = frame.contentWindow;
              }
              return ((document, window, location) => (EXPRESSION))(
                target.document, target, window.location);
            })()""".replace("FRAME_PATH", json.dumps(self.frame_path)).replace("EXPRESSION", expression)
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
        if self.frame_path:
            element = self.evaluate(f"document.querySelector({json.dumps(selector)})", return_by_value=False)
            object_id = element.get("objectId")
            if not object_id:
                raise CDPError(f"file input not found in application frame: {selector}")
            try:
                node = self.call("DOM.describeNode", {"objectId": object_id})["result"]["node"]
                self.call("DOM.setFileInputFiles", {"backendNodeId": node["backendNodeId"], "files": [file_path]})
            finally:
                self.call("Runtime.releaseObject", {"objectId": object_id})
            self._settle_upload(file_path)
            return
        root = self.call("DOM.getDocument", {"depth": -1, "pierce": True})["result"]["root"]["nodeId"]
        node = self.call("DOM.querySelector", {"nodeId": root, "selector": selector})["result"].get("nodeId")
        if not node:
            raise CDPError(f"file input not found: {selector}")
        self.call("DOM.setFileInputFiles", {"nodeId": node, "files": [file_path]})
        self._settle_upload(file_path)

    def _settle_upload(self, file_path: str) -> None:
        """Keep the dialog subscription alive until Clarivate accepts the file."""
        url = urllib.parse.urlsplit(self.evaluate('location.href'))
        if f'{url.scheme}://{url.netloc}' not in TRUSTED_ALERT_ORIGINS:
            return
        filename = os.path.basename(file_path)
        deadline = time.monotonic()+max(10, self.timeout*3)
        while time.monotonic() < deadline:
            accepted = self.evaluate(f"""(() => {{
              const shown=e=>e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
              return [...document.querySelectorAll('a')].some(e=>shown(e)&&
                /download/i.test(e.innerText)&&e.parentElement.innerText.includes({json.dumps(filename)}));
            }})()""")
            if accepted:
                return
            time.sleep(.15)
        raise BrowserTimeoutError('Resume acceptance is unverified; inspect the desktop without repeating the upload.')

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


def connect_first_page(
    host: str = "127.0.0.1",
    port: int = 9222,
    timeout: float = 10.0,
    *,
    expected_url: str | None = None,
) -> CDPClient:
    """Connect only when the application tab is unambiguous and loaded."""
    pages = [
        target for target in list_targets(host, port, timeout)
        if target.type == "page" and urllib.parse.urlsplit(target.url).scheme in {"http", "https"}
    ]
    if expected_url is not None:
        pages = [target for target in pages if target.url == expected_url]
    if not pages:
        raise CDPError("application page not found; open it in Chromium and inspect the current URL")
    if len(pages) != 1:
        raise CDPError("multiple application tabs found; select the exact page URL")
    return CDPClient(pages[0].websocket_url, timeout=timeout)
