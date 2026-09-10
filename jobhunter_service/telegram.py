"""Private Telegram transport for an owner-invited, restricted JobHunter service."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Protocol
from urllib.parse import urlsplit

import requests

from .hermes import HermesResponseError, RestrictedHermesAssistant, contains_credentials
from .resumes import MAX_UPLOAD_BYTES, ResumeImportError, import_resume

_API_ROOT = "https://api.telegram.org"
_ACTION_ID = re.compile(r"^[A-Za-z0-9_-]{12,48}$")
_MAX_PREVIEW_CHARS = 24_000


class JobHunterService(Protocol):
    def authorize(self, actor_id: int) -> None: ...
    def admin(self, actor_id: int, operation: str, target_id: int | None = None) -> dict: ...
    def snapshot(self, actor_id: int) -> dict: ...
    def propose(self, actor_id: int, patch: dict) -> dict: ...
    def confirm(self, actor_id: int, action_id: str) -> dict: ...
    def stage_resume(self, actor_id: int, resume: dict) -> dict: ...
    def connect(self, actor_id: int, provider: str, purpose: str) -> str: ...
    def get_update_offset(self) -> int: ...
    def acknowledge_update(self, update_id: int) -> None: ...
    def record_turn(self, actor_id: int, user_text: str, assistant_text: str) -> None: ...


class TelegramAPIError(RuntimeError):
    """A transport failure whose text never includes a token or request URL."""


def _integer(value, *, positive: bool = False) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and (not positive or value > 0)


class TelegramClient:
    def __init__(self, token: str, session=None, timeout: int = 20):
        if not isinstance(token, str) or not re.fullmatch(r"\d{4,20}:[A-Za-z0-9_-]{20,150}", token):
            raise ValueError("A valid JobHunter Telegram bot token is required.")
        self._token = token
        self._session = session or requests.Session()
        if hasattr(self._session, "trust_env"):
            self._session.trust_env = False
        self.timeout = timeout

    def _call(self, method: str, payload: dict, *, timeout: int | None = None):
        if method not in {"sendMessage", "answerCallbackQuery", "getUpdates", "getFile", "getChat", "getChatMember", "getMe"}:
            raise ValueError("Unsupported Telegram operation.")
        response = None
        try:
            response = self._session.post(
                f"{_API_ROOT}/bot{self._token}/{method}", json=payload,
                timeout=timeout or self.timeout, allow_redirects=False,
            )
            if response.status_code != 200:
                raise TelegramAPIError("Telegram did not acknowledge the request.")
            body = response.json()
            if not isinstance(body, dict) or body.get("ok") is not True or "result" not in body:
                raise TelegramAPIError("Telegram did not acknowledge the request.")
            return body["result"]
        except (requests.RequestException, ValueError):
            raise TelegramAPIError("Telegram request failed; no credentials were logged.") from None
        finally:
            if response is not None:
                response.close()

    def get_me(self) -> dict:
        result = self._call("getMe", {})
        if not isinstance(result, dict) or not _integer(result.get("id"), positive=True):
            raise TelegramAPIError("Telegram returned an invalid bot identity.")
        return result

    def send_message(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> dict:
        if not isinstance(text, str) or not text or len(text.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Telegram messages must contain at most 4096 characters.")
        payload = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", payload)
        if not isinstance(result, dict) or not _integer(result.get("message_id"), positive=True):
            raise TelegramAPIError("Telegram did not acknowledge message delivery.")
        return result

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        if not isinstance(callback_id, str) or not callback_id or len(callback_id) > 256:
            raise ValueError("Invalid callback identifier.")
        result = self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
        if result is not True:
            raise TelegramAPIError("Telegram did not acknowledge the callback.")

    def send_document(self, chat_id: int | str, path: str | Path, caption: str = "") -> dict:
        """Send a trusted, candidate-scoped file without following symlinks.

        Only backend code supplies this path. It is never accepted from a
        candidate message or model output. Opening through directory descriptors
        prevents a swapped symlink from redirecting the upload to owner files.
        """
        if not isinstance(caption, str) or len(caption.encode("utf-16-le")) // 2 > 1024:
            raise ValueError("Document captions must contain at most 1024 characters.")
        upload_path = Path(path).absolute()
        if ".." in upload_path.parts or not upload_path.name:
            raise ValueError("Invalid document path.")
        parent_fd = os.open(upload_path.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        file_fd, response = None, None
        try:
            for part in upload_path.parts[1:-1]:
                next_fd = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            file_fd = os.open(upload_path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 25 * 1024 * 1024:
                raise ValueError("Upload a nonempty document no larger than 25 MiB.")
            with os.fdopen(file_fd, "rb") as document:
                file_fd = None
                response = self._session.post(f"{_API_ROOT}/bot{self._token}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"document": (upload_path.name, document, "application/octet-stream")},
                    timeout=max(self.timeout, 60), allow_redirects=False)
            if response.status_code != 200:
                raise TelegramAPIError("Telegram did not acknowledge document delivery.")
            body = response.json()
            result = body.get("result") if isinstance(body, dict) and body.get("ok") is True else None
            if not isinstance(result, dict) or not _integer(result.get("message_id"), positive=True):
                raise TelegramAPIError("Telegram did not acknowledge document delivery.")
            return result
        except requests.RequestException:
            raise TelegramAPIError("Telegram document upload failed; no credentials were logged.") from None
        except OSError:
            raise ValueError("The document is unavailable or contains an unsafe path.") from None
        except ValueError as error:
            if response is not None:
                raise TelegramAPIError("Telegram did not acknowledge document delivery.") from None
            raise error
        finally:
            os.close(parent_fd)
            if file_fd is not None:
                os.close(file_fd)
            if response is not None:
                response.close()

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict]:
        if not _integer(offset) or offset < 0 or not _integer(timeout) or not 0 <= timeout <= 50:
            raise ValueError("Invalid Telegram polling parameters.")
        result = self._call("getUpdates", {"offset": offset, "timeout": timeout,
                                          "allowed_updates": ["message", "callback_query"], "limit": 50},
                            timeout=max(self.timeout, timeout + 10))
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise TelegramAPIError("Telegram returned invalid updates.")
        return result

    def download_document(self, document: dict, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
        if not isinstance(document, dict) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_UPLOAD_BYTES:
            raise ResumeImportError("Invalid resume attachment.")
        size = document.get("file_size")
        if size is not None and (not _integer(size, positive=True) or size > max_bytes):
            raise ResumeImportError("Upload a nonempty resume no larger than 8 MiB.")
        file_id = document.get("file_id")
        if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", file_id):
            raise ResumeImportError("Invalid resume attachment identifier.")
        metadata = self._call("getFile", {"file_id": file_id})
        if not isinstance(metadata, dict):
            raise TelegramAPIError("Telegram returned invalid attachment metadata.")
        path = metadata.get("file_path")
        size = metadata.get("file_size")
        if size is not None and (not _integer(size, positive=True) or size > max_bytes):
            raise ResumeImportError("The attachment exceeds the download limit.")
        if (not isinstance(path, str) or len(path) > 512 or
                not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)+", path) or
                any(part in {".", ".."} for part in path.split("/"))):
            raise TelegramAPIError("Telegram returned an invalid attachment path.")
        response = None
        try:
            response = self._session.get(f"{_API_ROOT}/file/bot{self._token}/{path}",
                                         timeout=self.timeout, allow_redirects=False, stream=True)
            if response.status_code != 200:
                raise TelegramAPIError("Telegram attachment download failed.")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    content_length = int(length)
                except (TypeError, ValueError):
                    raise TelegramAPIError("Telegram returned an invalid attachment length.") from None
                if content_length < 0 or content_length > max_bytes:
                    raise ResumeImportError("The attachment exceeds the download limit.")
            chunks = bytearray()
            deadline = time.monotonic() + 60
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if time.monotonic() > deadline:
                    raise TelegramAPIError("The attachment exceeded the download time limit.")
                if len(chunks) + len(chunk) > max_bytes:
                    raise ResumeImportError("The attachment exceeds the download limit.")
                chunks.extend(chunk)
            if not chunks:
                raise ResumeImportError("The attachment is empty.")
            return bytes(chunks)
        except requests.RequestException:
            raise TelegramAPIError("Telegram attachment download failed; no credentials were logged.") from None
        finally:
            if response is not None:
                response.close()

    def validate_destination(self, actor_id: int, destination: dict) -> dict:
        """Check both the candidate's control and the bot's ability to post."""
        if not _integer(actor_id, positive=True) or not isinstance(destination, dict):
            raise PermissionError("Invalid delivery destination.")
        chat_id = str(destination.get("chat_id", ""))
        if destination.get("kind") == "private":
            if chat_id != str(actor_id):
                raise PermissionError("Private delivery must use your own Telegram chat.")
            return dict(destination)
        if destination.get("kind") != "channel" or not re.fullmatch(r"-100\d{5,}", chat_id):
            raise PermissionError("Use the numeric ID of a channel you administer.")
        chat = self._call("getChat", {"chat_id": chat_id})
        if not isinstance(chat, dict) or chat.get("type") != "channel" or str(chat.get("id")) != chat_id:
            raise PermissionError("The delivery destination must be a Telegram channel.")
        actor = self._call("getChatMember", {"chat_id": chat_id, "user_id": actor_id})
        if not isinstance(actor, dict) or actor.get("status") not in {"creator", "administrator"}:
            raise PermissionError("You must administer that Telegram channel.")
        bot = self._call("getChatMember", {"chat_id": chat_id, "user_id": self.get_me()["id"]})
        if not isinstance(bot, dict) or bot.get("status") not in {"creator", "administrator"} or (
                bot.get("status") != "creator" and bot.get("can_post_messages") is not True):
            raise PermissionError("Add the JobHunter bot as a channel administrator with posting permission.")
        return {**destination, "chat_id": chat_id, "label": str(chat.get("title", destination.get("label", "Jobs")))[:120]}


def message_chunks(text: str, limit: int = 3000) -> list[str]:
    """Split without hiding any preview content, accounting for UTF-16 units."""
    chunks, chunk, count = [], [], 0
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if count + units > limit:
            chunks.append("".join(chunk))
            chunk, count = [], 0
        chunk.append(char)
        count += units
    if chunk:
        chunks.append("".join(chunk))
    return chunks


def _private_actor(update: dict) -> tuple[int, dict, dict | None] | None:
    callback = update.get("callback_query")
    if callback is not None:
        if not isinstance(callback, dict):
            return None
        message, sender = callback.get("message"), callback.get("from")
    else:
        message = update.get("message")
        sender = message.get("from") if isinstance(message, dict) else None
    if not isinstance(message, dict) or not isinstance(sender, dict) or sender.get("is_bot") is True:
        return None
    actor_id, chat = sender.get("id"), message.get("chat")
    if (not _integer(actor_id, positive=True) or not isinstance(chat, dict) or chat.get("type") != "private"
            or not _integer(chat.get("id"), positive=True) or chat["id"] != actor_id):
        return None
    return actor_id, message, callback


class TelegramHandler:
    def __init__(self, service: JobHunterService, client: TelegramClient, assistant=None):
        self.service, self.client = service, client
        self.assistant = assistant or RestrictedHermesAssistant()

    def _send(self, actor_id: int, text: str) -> None:
        for chunk in message_chunks(text):
            self.client.send_message(actor_id, chunk)

    def _preview(self, actor_id: int, proposal: dict) -> None:
        action_id, preview = proposal.get("action_id"), proposal.get("preview")
        if not isinstance(action_id, str) or not _ACTION_ID.fullmatch(action_id):
            raise ValueError("The settings preview has an invalid confirmation identifier.")
        if not isinstance(preview, str) or not preview.strip() or len(preview) > _MAX_PREVIEW_CHARS:
            raise ValueError("This change is too large to review in Telegram. Propose one section at a time.")
        self._send(actor_id, "Review the complete proposed changes:\n" + preview)
        # If any preceding message fails, there is no confirmation button.
        self.client.send_message(actor_id, "Save these changes? Resume facts are confirmed only if you approve their exact public wording.", {
            "inline_keyboard": [[{"text": "Confirm changes", "callback_data": "jh:confirm:" + action_id}]]
        })

    def _execute(self, actor_id: int, plan: dict) -> None:
        if plan["operation"] == "propose":
            self._preview(actor_id, self.service.propose(actor_id, plan["patch"]))
        elif plan["operation"] == "connect":
            link = self.service.connect(actor_id, plan["provider"], plan["purpose"])
            parsed = urlsplit(link)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("The connection service did not return a secure link.")
            self.client.send_message(actor_id, "Open your private connection link. Enter credentials only on the account provider's page; never send them to Telegram.", {
                "inline_keyboard": [[{"text": "Connect " + plan["provider"].title(), "url": link}]]
            })
        elif plan["operation"] == "show":
            state = self.service.snapshot(actor_id)
            # The facade owns snapshot sanitization. Do not expose uploaded
            # source text or conversational history in a settings summary.
            visible = {key: value for key, value in state.items()
                       if key not in {"history", "conversation", "resume_source", "resume_draft"}}
            self._send(actor_id, json.dumps(visible, ensure_ascii=False, indent=2, default=str))
        else:
            self._send(actor_id, plan["reply"])

    def _handle_private(self, actor_id: int, message: dict, callback: dict | None) -> None:
        text = message.get("text", "")
        if not isinstance(text, str):
            text = ""
        if callback is None and re.match(r"^/jobhunter(?:@\w+)?(?:\s|$)", text, re.I):
            parts = text.split()
            if len(parts) == 2 and parts[1].lower() == "list":
                result = self.service.admin(actor_id, "list", None)
            elif len(parts) == 3 and parts[1].lower() in {"add", "suspend", "revoke"} and re.fullmatch(r"[1-9]\d{0,18}", parts[2]):
                result = self.service.admin(actor_id, parts[1].lower(), int(parts[2]))
            else:
                raise ValueError("Use /jobhunter add <telegram_user_id>, list, suspend <id>, or revoke <id>.")
            self._send(actor_id, json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return
        self.service.authorize(actor_id)
        if callback is not None:
            data = callback.get("data")
            if not isinstance(data, str) or not data.startswith("jh:confirm:") or not _ACTION_ID.fullmatch(data[11:]):
                raise ValueError("This action is unavailable. Use /status to continue.")
            self.service.confirm(actor_id, data[11:])
            try:
                self.client.answer_callback(callback["id"], "Changes saved")
            except TelegramAPIError:
                # Telegram expires callback acknowledgements quickly. Saving
                # succeeded, so still send the durable outcome to the chat.
                pass
            self._send(actor_id, "Your changes are saved. Use /status to see your settings or tell me the next change.")
            return
        if text.startswith("/confirm"):
            parts = text.split()
            if len(parts) != 2 or parts[0].split("@", 1)[0] != "/confirm" or not _ACTION_ID.fullmatch(parts[1]):
                raise ValueError("Use the confirmation button below the complete settings preview.")
            self.service.confirm(actor_id, parts[1])
            self._send(actor_id, "Your changes are saved. Use /status to see your settings.")
            return
        if "document" in message:
            document = message["document"]
            if not isinstance(document, dict):
                raise ResumeImportError("Invalid resume attachment.")
            filename = document.get("file_name", "")
            if not isinstance(filename, str) or Path(filename).suffix.lower() not in {".pdf", ".docx", ".txt"}:
                raise ResumeImportError("Upload a PDF, DOCX, or UTF-8 text resume.")
            source = import_resume(self.client.download_document(document), filename, document.get("mime_type"))
            self.service.stage_resume(actor_id, source)
            self._send(actor_id, "Your resume was imported as an unconfirmed draft. I will review one experience at a time and ask you to confirm exact public wording before using any facts.")
            plan = self.assistant.plan(
                "I uploaded my resume. Begin refinement with one focused question about my first experience. Do not confirm any facts.",
                self.service.snapshot(actor_id))
            self._execute(actor_id, plan)
            self.service.record_turn(actor_id, "Uploaded resume: " + source["filename"],
                                     plan.get("reply", "Resume changes await review and confirmation."))
            return
        if not text:
            self._send(actor_id, "Send a text message or upload your resume as PDF, DOCX, or text.")
            return
        plan = self.assistant.plan(text, self.service.snapshot(actor_id))
        self._execute(actor_id, plan)
        if not contains_credentials(text):
            self.service.record_turn(actor_id, text, plan.get("reply", {
                "propose": "Proposed changes await review and confirmation.",
                "connect": "A private account connection link was provided.",
                "show": "Displayed the candidate's current JobHunter settings.",
            }.get(plan["operation"], "")))

    def handle_update(self, update: dict, *, use_offset: bool = True) -> None:
        """Handle one update obtained from the authenticated Bot API poller.

        This is not an unauthenticated webhook endpoint. The persistent offset
        rejects redelivery after a completed update. Backend confirmations must
        also be idempotent to cover crashes after applying an action. Durable
        shared-bot ingress uses its own per-update receipts and passes
        ``use_offset=False`` so reordered forwarded updates are not discarded.
        """
        if not isinstance(update, dict) or not _integer(update.get("update_id")) or update["update_id"] < 0:
            return
        update_id = update["update_id"]
        if use_offset and update_id < self.service.get_update_offset():
            return
        private = _private_actor(update)
        if private is None:
            if use_offset:
                self.service.acknowledge_update(update_id)
            return
        actor_id, message, callback = private
        try:
            self._handle_private(actor_id, message, callback)
        except PermissionError:
            self._send(actor_id, "You do not have access to that JobHunter action. Ask the owner to authorize your Telegram user ID: " + str(actor_id))
        except (ValueError, ResumeImportError, HermesResponseError) as error:
            explanation = str(error)
            if contains_credentials(explanation) or "api.telegram.org" in explanation or len(explanation) > 600:
                explanation = "The request could not be processed. Your credentials were not shared. Use /status to continue."
            self._send(actor_id, explanation)
        # Network/transient failures deliberately do not acknowledge an update.
        if use_offset:
            self.service.acknowledge_update(update_id)

    def poll_once(self) -> int:
        updates = self.client.get_updates(self.service.get_update_offset())
        for update in sorted(updates, key=lambda item: item.get("update_id", -1)):
            self.handle_update(update)
        return len(updates)

    def run(self, lock_path: str | Path, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or threading.Event()
        with polling_lock(lock_path):
            while not stop.is_set():
                try:
                    self.poll_once()
                except TelegramAPIError:
                    # A bounded interruptible backoff; no URLs or tokens logged.
                    stop.wait(5)


@contextmanager
def polling_lock(path: str | Path):
    """A local single-poller lease held for the lifetime of the service."""
    path = Path(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another JobHunter Telegram poller owns this service.") from None
        yield
    finally:
        os.close(descriptor)


def handle_update(update: dict, service: JobHunterService, client=None, assistant=None, *, use_offset=True) -> None:
    TelegramHandler(service, client or service.telegram_client, assistant).handle_update(update, use_offset=use_offset)
