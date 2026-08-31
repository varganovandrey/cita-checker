"""Notifications: Telegram (stdlib urllib, sync) and local Windows alert.

Notification failures are logged and swallowed — they must never kill the monitor loop.
Console/log output is English ASCII only; Telegram message text may be Russian.
"""

import ctypes
import json
import logging
import mimetypes
import re
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("monitor.notify")

TELEGRAM_TIMEOUT = 10


def _telegram_creds() -> Optional[tuple[str, str]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return token, chat_id


def send_telegram(text: str, photo: Optional[Path] = None) -> bool:
    """Send a Telegram message, optionally with a photo attached.

    Args:
        text: Message text (HTML parse mode).
        photo: Optional path to an image to send via sendPhoto.

    Returns:
        True if the text message was delivered.
    """
    creds = _telegram_creds()
    if creds is None:
        logger.warning("Telegram credentials missing (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return False
    token, chat_id = creds

    ok = _post_message(token, chat_id, text)
    if photo is not None and photo.exists():
        _post_photo(token, chat_id, photo, caption=None)
    return ok


def _post_message(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    """Send one message; on an HTML parse failure, resend as plain text.

    Alerts carry error text verbatim, and Playwright's messages contain things
    like "<ws connecting>". Unescaped, those make Telegram reject the whole
    message with a 400 - losing the alert at the exact moment it matters most.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    fields = {"chat_id": chat_id, "text": text}
    if parse_mode:
        fields["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(fields).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=data, timeout=TELEGRAM_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                logger.warning("Telegram sendMessage rejected: %s", body)
                return False
            logger.info("Telegram message sent")
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 400 and parse_mode:
            logger.warning("Telegram rejected the markup, resending as plain text")
            return _post_message(token, chat_id, _strip_tags(text), parse_mode="")
        logger.warning("Telegram sendMessage failed: %s", exc)
        return False
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("Telegram sendMessage failed: %s", exc)
        return False


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]{0,40}>", "", text)


def _post_photo(token: str, chat_id: str, photo: Path, caption: Optional[str]) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    boundary = uuid.uuid4().hex
    content_type = mimetypes.guess_type(photo.name)[0] or "image/png"

    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    add_field("chat_id", chat_id)
    if caption:
        add_field("caption", caption)
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="{photo.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    try:
        parts.append(photo.read_bytes())
    except OSError as exc:
        logger.warning("Cannot read screenshot %s: %s", photo, exc)
        return False
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT * 3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if not payload.get("ok"):
                logger.warning("Telegram sendPhoto rejected: %s", payload)
                return False
            logger.info("Telegram photo sent")
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("Telegram sendPhoto failed: %s", exc)
        return False


def local_alert(title: str, message: str) -> None:
    """Beep pattern + Windows MessageBox on daemon threads (non-blocking)."""
    try:
        import winsound

        def beep() -> None:
            for _ in range(6):
                winsound.Beep(1200, 220)
                winsound.Beep(880, 220)

        threading.Thread(target=beep, daemon=True).start()
        threading.Thread(
            target=lambda: ctypes.windll.user32.MessageBoxW(0, message, title, 0x1000 | 0x40),
            daemon=True,
        ).start()
        logger.info("Local alert fired")
    except (ImportError, OSError, AttributeError) as exc:
        logger.warning("Local alert failed: %s", exc)
