"""Notifications: a Windows toast on this machine, a push to your phone.

Two transports because they answer different questions. A toast reaches you
when you are at the desk; ntfy reaches you when you are not. A scheduled task
that finished while you were out is worthless if the only record is a toast
nobody saw.

ntfy is the free choice on purpose: publishing to a topic is an unauthenticated
HTTP POST to a public server, so there is no account, no API key, and no
vendor. The cost of that simplicity is that **a topic name is a password** -
anyone who knows it can read your notifications. The topic therefore lives in
the credential store, not in a config file, and defaults to off.

Delivery is best-effort by design: a notification that fails must never fail
the task that produced it. Every send reports which transports worked, and the
event log records it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import httpx
import keyring

from myagent.logging import get_logger

log = get_logger(__name__)

KEYRING_SERVICE = "myagent"
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC_REF = "ntfy_topic"  # credential-store key
SEND_TIMEOUT_S = 10.0
TOAST_APP_NAME = "MyAgent"
MAX_BODY_CHARS = 400  # both transports truncate; do it ourselves, visibly


@dataclass
class Delivery:
    """What actually happened when a notification was sent."""

    toast: bool = False
    push: bool = False
    errors: list[str] | None = None

    @property
    def delivered(self) -> bool:
        """True if the user was reached by at least one route."""
        return self.toast or self.push

    def as_dict(self) -> dict[str, Any]:
        """Serializable form for the API and the event log."""
        return {
            "toast": self.toast,
            "push": self.push,
            "delivered": self.delivered,
            "errors": self.errors or [],
        }


def ntfy_topic() -> str | None:
    """The configured ntfy topic, or None when phone push is off."""
    return keyring.get_password(KEYRING_SERVICE, NTFY_TOPIC_REF) or None


def set_ntfy_topic(topic: str) -> None:
    """Store the ntfy topic in Windows Credential Manager, never a file."""
    cleaned = topic.strip()
    if not cleaned:
        raise ValueError("topic is empty")
    keyring.set_password(KEYRING_SERVICE, NTFY_TOPIC_REF, cleaned)


def send_toast(title: str, body: str) -> None:
    """Show a Windows notification. Raises if the OS refuses."""
    if sys.platform != "win32":
        raise RuntimeError("toasts are Windows-only")
    from winotify import Notification

    notification = Notification(
        app_id=TOAST_APP_NAME,
        title=title[:64],
        msg=body[:MAX_BODY_CHARS],
    )
    notification.show()


def send_push(title: str, body: str, topic: str, priority: str = "default") -> None:
    """Publish to an ntfy topic. Raises on any non-2xx response."""
    response = httpx.post(
        f"{NTFY_SERVER}/{topic}",
        content=body[:MAX_BODY_CHARS].encode("utf-8"),
        headers={
            "Title": title[:64].encode("ascii", "replace").decode("ascii"),
            "Priority": priority,
        },
        timeout=SEND_TIMEOUT_S,
    )
    response.raise_for_status()


def send(title: str, body: str, priority: str = "default", push: bool = True) -> Delivery:
    """Notify the user by every route available, reporting what worked.

    Never raises: a failed notification must not take down the task that
    triggered it. Callers that care can check ``Delivery.delivered``.
    """
    result = Delivery(errors=[])
    assert result.errors is not None

    try:
        send_toast(title, body)
        result.toast = True
    except Exception as exc:
        result.errors.append(f"toast: {exc}")

    topic = None
    if push:
        try:
            topic = ntfy_topic()
        except Exception as exc:
            result.errors.append(f"ntfy topic: {exc}")
    if topic:
        try:
            send_push(title, body, topic, priority)
            result.push = True
        except Exception as exc:
            result.errors.append(f"ntfy: {exc}")

    log.info(
        "notified",
        title=title[:40],
        toast=result.toast,
        push=result.push,
        errors=len(result.errors),
    )
    return result
