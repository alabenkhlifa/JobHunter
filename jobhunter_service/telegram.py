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

from .hermes import HELP_TEXT, HermesResponseError, HermesUnavailableError, RestrictedHermesAssistant, contains_credentials
from .resumes import MAX_UPLOAD_BYTES, ResumeImportError, import_resume

_API_ROOT = "https://api.telegram.org"
_ACTION_ID = re.compile(r"^[A-Za-z0-9_-]{12,48}$")
_MAX_PREVIEW_CHARS = 24_000
_ONBOARDING = re.compile(r"^jh:onboard:(acknowledge|skip|reopen|activate|check):(resume|roles|markets|schedule|delivery|linkedin|gmail|tracker|review):([0-9]{1,20})$")
_RETRY = re.compile(r"^jh:retry:(JH-[A-F0-9]{12})$")


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
    def onboarding_status(self, actor_id: int, *, start: bool = False) -> dict: ...
    def onboarding_action(self, actor_id: int, action: str, step: str, revision: int) -> dict: ...


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


def readable_fields(value, *, depth=0) -> str:
    """Render every supplied value; lists explicitly replace their previous contents."""
    prefix = "  " * depth
    if isinstance(value, dict):
        if not value:
            return prefix + "No fields supplied."
        lines = []
        for key, item in value.items():
            label = str(key).replace("_", " ")
            if isinstance(item, (dict, list)):
                lines.extend([prefix + label + ":", readable_fields(item, depth=depth + 1)])
            else:
                rendered = "Yes" if item is True else "No" if item is False else "Not set" if item is None else str(item) if item != "" else "(empty text)"
                lines.append(prefix + label + ": " + rendered.replace("\n", "\n" + prefix + "  "))
        return "\n".join(lines)
    if isinstance(value, list):
        lines = [prefix + "Replace the previous list with these entries:" if value else prefix + "Replace the previous list with no entries."]
        for index, item in enumerate(value, 1):
            lines.append(prefix + str(index) + ". " + ("" if isinstance(item, (dict, list)) else str(item)))
            if isinstance(item, (dict, list)):
                lines.append(readable_fields(item, depth=depth + 1))
        return "\n".join(lines)
    return prefix + str(value)


class TelegramHandler:
    def __init__(self, service: JobHunterService, client: TelegramClient, assistant=None):
        self.service, self.client = service, client
        self.assistant = assistant or RestrictedHermesAssistant()

    def _send(self, actor_id: int, text: str) -> None:
        for chunk in message_chunks(text):
            self.client.send_message(actor_id, chunk)

    def _preview(self, actor_id: int, proposal: dict) -> None:
        action_id, preview = proposal.get("action_id"), proposal.get("preview")
        if isinstance(proposal.get("patch"), dict):
            summary = proposal.get("summary", "")
            if not isinstance(summary, str):
                raise ValueError("The settings summary is invalid. Request a fresh preview.")
            preview = (summary + "\n\n" if summary else "") + "Exact changes:\n" + readable_fields(proposal["patch"])
            if "next_run" in proposal:
                preview += "\nNext search: " + str(proposal["next_run"] or "paused")
        if not isinstance(action_id, str) or not _ACTION_ID.fullmatch(action_id):
            raise ValueError("The settings preview has an invalid confirmation identifier.")
        if not isinstance(preview, str) or not preview.strip() or len(preview) > _MAX_PREVIEW_CHARS:
            raise ValueError("This change is too large to review in Telegram. Propose one section at a time.")
        self._send(actor_id, "Your turn: Review the complete proposed changes:\n" + preview)
        # If any preceding message fails, there is no confirmation button.
        self.client.send_message(actor_id, "Your turn: Tap Confirm changes to save this wording, or reply with corrections. "
                                 "Resume facts are confirmed only if you approve their exact public wording.", {
            "inline_keyboard": [[{"text": "Confirm changes", "callback_data": "jh:confirm:" + action_id}]]
        })

    def _onboarding(self, actor_id, *, start=False):
        method = getattr(self.service, "onboarding_status", None)
        return method(actor_id, start=start) if callable(method) else None

    def _context(self, actor_id):
        state = self.service.snapshot(actor_id)
        onboarding = self._onboarding(actor_id)
        if onboarding is not None:
            state["onboarding"] = onboarding
        connections = getattr(self.service, "connection_status", None)
        if callable(connections):
            state["connection_status"] = connections(actor_id)
        return state

    def _guide(self, actor_id, state=None, *, checklist=False, introduction=""):
        state = state if state is not None else self._onboarding(actor_id)
        if not isinstance(state, dict):
            self._send(actor_id, introduction + "\nUse /status to check your settings or tell me the next change.")
            return
        lines = [introduction] if introduction else []
        steps = state.get("steps", [])
        if checklist:
            lines.append("Your setup checklist")
            for step in steps:
                marker = {"complete": "✓", "skipped": "—", "blocked": "!"}.get(step["state"], "○")
                lines.append(f"{marker} {step['label']}: {step['state']}")
        if not state.get("started"):
            lines.append("Use /onboarding to review your setup one step at a time. Running searches change only after you confirm a schedule change.")
        elif state.get("complete"):
            lines.append("Your guided setup is complete. Use /status to check your schedule, /jobs for results, or describe a change.")
        elif state.get("next_question"):
            lines.append("Your turn — Next: " + state["next_question"])
        current = next((step for step in steps if step["id"] == state.get("next_step")), None)
        if current and current.get("detail"):
            lines.append(current["label"] + "\n" + current["detail"])
        self._send(actor_id, "\n\n".join(lines) or "Use /onboarding to begin your saved setup.")
        if not state.get("started") or not current:
            return
        labels = {"acknowledge": {"resume": "Done reviewing", "roles": "Keep current limits", "schedule": "Use this schedule",
                                   "delivery": "Use these delivery chats"}.get(current["id"], "Confirm this step"),
                  "skip": "Skip for now", "check": "Check connection", "reopen": "Review again", "activate": "Review activation"}
        buttons = [{"text": labels[action], "callback_data": f"jh:onboard:{action}:{current['id']}:{state['revision']}"}
                   for action in current.get("actions", []) if action in labels]
        if buttons:
            self.client.send_message(actor_id, "Your turn: Choose a button below, or reply in your own words. I’m waiting for you.",
                                     {"inline_keyboard": [[button] for button in buttons]})

    def _resume_review_choices(self, actor_id):
        state = self._onboarding(actor_id)
        if not isinstance(state, dict) or state.get("next_step") != "resume":
            return
        current = next((step for step in state.get("steps", []) if step["id"] == "resume"), {})
        if "acknowledge" in current.get("actions", []):
            self.client.send_message(actor_id, "Your turn: Answer the question above. If you have finished reviewing all "
                "experiences or want to stop refinement, tap Done reviewing to continue setup.", {
                "inline_keyboard": [[{"text": "Done reviewing",
                    "callback_data": f"jh:onboard:acknowledge:resume:{state['revision']}"}]]})

    def _after_confirmation(self, actor_id, result):
        state = self._onboarding(actor_id)
        receipt = result.get("confirmation", {}) if isinstance(result, dict) else {}
        if (receipt.get("newly_applied") is True and receipt.get("resume_changed") is True
                and isinstance(state, dict) and state.get("started") and state.get("next_step") == "resume"):
            from .recovery import RESUME_FOLLOWUP_INTENT
            self._plan(actor_id, RESUME_FOLLOWUP_INTENT, kind="resume_followup",
                       waiting="Your changes are saved. I’m preparing your next interview question. "
                               "Please wait—I’ll message you when it’s your turn.")
        else:
            self._guide(actor_id, state, introduction="Your changes are saved.")

    def _status(self, actor_id):
        snapshot = self.service.snapshot(actor_id)
        settings = snapshot.get("settings", snapshot.get("config", {}))
        schedule = settings.get("schedule", {})
        search = settings.get("search", {})
        roles = search.get("matching", {}).get("preferred_roles", []) or search.get("keywords", [])
        lines = ["Your JobHunter status", "Searches: " + ("running" if schedule.get("enabled") else "paused")]
        if schedule.get("time"):
            lines.append(f"Schedule: {schedule['time']} ({schedule.get('timezone', 'timezone not set')})")
        if snapshot.get("next_run"):
            lines.append("Next search: " + str(snapshot["next_run"]))
        lines.append("Roles: " + (", ".join(roles) if roles else "not set"))
        lines.append("Destinations: " + (", ".join(market.get("name", "Unnamed") for market in search.get("markets", [])) or "not set"))
        connections = getattr(self.service, "connection_status", None)
        if callable(connections):
            for provider, details in connections(actor_id).items():
                if provider in {"linkedin", "gmail", "tracker"}:
                    lines.append(provider.title() + ": " + details.get("message", details.get("status", "unverified")))
            lines.append("Use /check linkedin, /check gmail or /check tracker to verify a connection now.")
        self._guide(actor_id, checklist=True, introduction="\n".join(lines))

    def _plan(self, actor_id, text, *, kind="message", recovery=None, waiting=None, announce=True):
        from .recovery import clear_recovery, save_recovery
        context = self._context(actor_id)
        if kind == "resume_followup" and context.get("onboarding", {}).get("next_step") != "resume":
            self._guide(actor_id, introduction="Your saved resume is safe. Continue your current setup step below.")
            if recovery:
                clear_recovery(self.service, actor_id, recovery["reference"])
            return
        waiting = waiting or ("I’m preparing your next interview question. Please wait." if kind == "resume_followup" else
                              "Thanks. I’m reviewing your message and preparing the next step. Please wait.")
        try:
            plan = self.assistant.plan(text, context,
                on_wait=(lambda: self._send(actor_id, waiting)) if announce else None,
                reply_only=kind == "resume_followup")
        except (HermesUnavailableError, HermesResponseError) as error:
            if isinstance(error, HermesResponseError) and kind != "resume_followup":
                raise
            saved = save_recovery(self.service, actor_id, text, kind=kind)
            draft = "Your resume draft is saved; you do not need to upload it again. " if kind == "resume" else "Your saved settings are safe. "
            self.client.send_message(actor_id, "I couldn’t finish this step. The conversation service is temporarily unavailable. " + draft +
                       "Your turn: Tap Retry this request or use /retry. You can also use /status for the checklist or /support for a report.\n"
                       "Support reference: " + saved["reference"], {"inline_keyboard": [[{
                           "text": "Retry this request", "callback_data": "jh:retry:" + saved["reference"]}]]})
            return
        self._execute(actor_id, plan)
        if not contains_credentials(text):
            self.service.record_turn(actor_id, text if kind == "message" else
                                     "Resume changes confirmed; continue the interview" if kind == "resume_followup" else "Uploaded resume for refinement",
                                     plan.get("reply", {"propose": "Proposed changes await exact confirmation.",
                                                        "connect": "A private connection link was provided.",
                                                        "show": "Displayed the current setup checklist."}.get(plan["operation"], "")))
        if recovery:
            clear_recovery(self.service, actor_id, recovery["reference"])

    def _support(self, actor_id):
        from .recovery import get_recovery
        recovery = get_recovery(self.service, actor_id)
        state = self._onboarding(actor_id) or {}
        report = ["JobHunter support report (share this with the owner if you want help)",
                  f"Telegram user ID: {actor_id}", "Setup step: " + str(state.get("next_step") or "not started"),
                  "Setup revision: " + str(state.get("revision", "unknown"))]
        if recovery:
            report.extend(["Reference: " + recovery["reference"], "Issue: conversation temporarily unavailable",
                           "Retry attempts: " + str(recovery["attempts"])])
        else:
            report.append("No saved conversation failure. Describe the failed step when sharing this report.")
        connections = getattr(self.service, "connection_status", None)
        if callable(connections):
            for provider, details in connections(actor_id).items():
                if provider in {"linkedin", "gmail", "tracker"} and details.get("status") in {
                        "connected", "configured", "unverified", "unavailable", "error"}:
                    report.append(provider.title() + ": " + details["status"])
        report.append("This report contains no resume, message history or credentials. It has not been sent to anyone else.")
        self._send(actor_id, "\n".join(report))

    def _execute(self, actor_id: int, plan: dict) -> None:
        if plan["operation"] == "propose":
            self._preview(actor_id, self.service.propose(actor_id, plan["patch"]))
        elif plan["operation"] == "connect":
            link = self.service.connect(actor_id, plan["provider"], plan["purpose"])
            parsed = urlsplit(link)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("The connection service did not return a secure link.")
            label = "LinkedIn" if plan["provider"] == "linkedin" else "Gmail" if plan["purpose"] == "gmail" else "tracker"
            self.client.send_message(actor_id, "Your turn: Open your private connection link. Enter credentials only on the account provider's page; never send them to Telegram. "
                                     "After signing in, return here and use /check " + ("linkedin" if plan["provider"] == "linkedin" else plan["purpose"]) + " to verify the connection.", {
                "inline_keyboard": [[{"text": "Connect " + label, "url": link}]]
            })
        elif plan["operation"] == "show":
            self._status(actor_id)
        else:
            self._send(actor_id, "Your turn:\n" + plan["reply"])
            self._resume_review_choices(actor_id)

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
            retry = _RETRY.fullmatch(data) if isinstance(data, str) else None
            if retry:
                from .recovery import begin_retry, callback_retry_id, get_recovery
                pending = get_recovery(self.service, actor_id)
                if not pending or pending["reference"] != retry[1]:
                    raise ValueError("This retry expired or belongs to another request. Use /status to continue.")
                recovery = begin_retry(self.service, actor_id, request_id=callback_retry_id(callback["id"]), reference=retry[1])
                self._plan(actor_id, recovery["text"], kind=recovery["kind"], recovery=recovery)
                try:
                    self.client.answer_callback(callback["id"], "Retry processed")
                except TelegramAPIError:
                    pass
                return
            guided = _ONBOARDING.fullmatch(data) if isinstance(data, str) else None
            if guided:
                action, step, revision = guided.groups()
                if action == "check":
                    self._send(actor_id, f"I’m checking your {step.title()} connection. Please wait—I’ll send the result here.")
                result = self.service.onboarding_action(actor_id, action, step, int(revision))
                if "action_id" in result:
                    self._preview(actor_id, result)
                else:
                    self._guide(actor_id, result)
                try:
                    self.client.answer_callback(callback["id"], "Step checked")
                except TelegramAPIError:
                    pass
                return
            if not isinstance(data, str) or not data.startswith("jh:confirm:") or not _ACTION_ID.fullmatch(data[11:]):
                raise ValueError("This action is unavailable. Use /status to continue.")
            result = self.service.confirm(actor_id, data[11:])
            try:
                self.client.answer_callback(callback["id"], "Changes saved")
            except TelegramAPIError:
                # Telegram expires callback acknowledgements quickly. Saving
                # succeeded, so still send the durable outcome to the chat.
                pass
            self._after_confirmation(actor_id, result)
            return
        command = text.strip().split(" ", 1)[0].split("@", 1)[0].lower()
        if command in {"/start", "/onboarding", "/continue"}:
            state = self._onboarding(actor_id, start=True)
            running = self.service.snapshot(actor_id).get("settings", {}).get("schedule", {}).get("enabled", False)
            self._guide(actor_id, state, introduction="Welcome to your private JobHunter setup. You can stop and return with /continue. "
                        "We will confirm exact changes before saving. Never send passwords or verification codes here.\n"
                        + ("Your existing searches remain running. Schedule changes need your confirmation." if running else
                           "Searches are paused until you review and confirm activation."))
            return
        if command == "/help":
            self._send(actor_id, HELP_TEXT)
            return
        if command in {"/status", "/settings", "/profile"}:
            self._status(actor_id)
            return
        if command == "/support":
            self._support(actor_id)
            return
        if command == "/check":
            parts = text.split()
            if len(parts) != 2 or parts[1].lower() not in {"linkedin", "gmail", "tracker"}:
                raise ValueError("Use /check linkedin, /check gmail or /check tracker.")
            self._send(actor_id, f"I’m checking your {parts[1].title()} connection. Please wait—I’ll send the result here.")
            self.service.check_connection(actor_id, parts[1].lower())
            self._status(actor_id)
            return
        if command == "/retry":
            from .recovery import begin_retry
            recovery = begin_retry(self.service, actor_id, request_id=message.get("message_id"))
            self._plan(actor_id, recovery["text"], kind=recovery["kind"], recovery=recovery)
            return
        if command == "/resume":
            state = self._onboarding(actor_id)
            if state and state.get("started") and not state.get("complete"):
                self._guide(actor_id, state)
                return
        if text.startswith("/confirm"):
            parts = text.split()
            if len(parts) != 2 or parts[0].split("@", 1)[0] != "/confirm" or not _ACTION_ID.fullmatch(parts[1]):
                raise ValueError("Use the confirmation button below the complete settings preview.")
            result = self.service.confirm(actor_id, parts[1])
            self._after_confirmation(actor_id, result)
            return
        if "document" in message:
            document = message["document"]
            if not isinstance(document, dict):
                raise ResumeImportError("Invalid resume attachment.")
            filename = document.get("file_name", "")
            if not isinstance(filename, str) or Path(filename).suffix.lower() not in {".pdf", ".docx", ".txt"}:
                raise ResumeImportError("Upload a PDF, DOCX, or UTF-8 text resume.")
            self._send(actor_id, "Resume received. I’m reading it and preparing your first question. "
                       "Please wait—I’ll message you when it’s your turn. "
                       "Its contents remain an unconfirmed draft until you approve the wording.")
            source = import_resume(self.client.download_document(document), filename, document.get("mime_type"))
            self.service.stage_resume(actor_id, source)
            snapshot = self.service.snapshot(actor_id)
            if not snapshot.get("settings", {}).get("schedule", {}).get("enabled"):
                self._onboarding(actor_id, start=True)
            self._plan(actor_id, "I uploaded my resume. Begin refinement with one focused question about my first experience. Do not confirm any facts.", kind="resume", announce=False)
            return
        if not text:
            self._send(actor_id, "Send a text message or upload your resume as PDF, DOCX, or text.")
            return
        self._plan(actor_id, text)

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
            self._send(actor_id, "You do not have access to that JobHunter action. Use /status to check your own setup. "
                       "If you have not been invited, ask the owner to authorize your Telegram user ID: " + str(actor_id))
        except (ValueError, ResumeImportError, HermesResponseError) as error:
            explanation = str(error)
            if (contains_credentials(explanation) or "api.telegram.org" in explanation or len(explanation) > 600
                    or re.search(r"/(?:home|etc|opt|Users|var)/|Traceback|Bearer\s", explanation)):
                explanation = "The request could not be processed. Your credentials were not shared. Use /status to continue."
            self._send(actor_id, "Your turn: " + explanation + "\nUse /status for your current step or /support for a safe report.")
            if callback is not None:
                self._guide(actor_id)
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
