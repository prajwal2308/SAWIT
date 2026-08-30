"""Push the takeaway to the phone.

ntfy is a stand-in: it gives a real lock-screen notification today without an
App Store account. When this becomes a real iOS app, this module is the only
thing that changes — swap it for APNs and everything upstream is untouched.
"""

from __future__ import annotations

import logging

import httpx

from .schemas import ReelNote

log = logging.getLogger(__name__)


def push_note(note: ReelNote, *, server: str, topic: str, click_url: str | None = None) -> None:
    body = note.one_liner
    if note.takeaways:
        body += "\n\n" + "\n".join(f"• {t}" for t in note.takeaways[:3])
    if note.steps:
        body += f"\n\n{len(note.steps)} steps saved."
    _send(server, topic, title=note.title, body=body, tags="memo", click_url=click_url)


def push_failure(url: str, error: str, *, server: str, topic: str) -> None:
    _send(
        server,
        topic,
        title="Could not save that reel",
        body=f"{error}\n\n{url}",
        tags="warning",
        click_url=None,
        priority="low",
    )


def _send(
    server: str,
    topic: str,
    *,
    title: str,
    body: str,
    tags: str,
    click_url: str | None,
    priority: str = "default",
) -> None:
    headers = {
        # ntfy reads these as latin-1; drop anything it would choke on rather
        # than lose the whole notification to an emoji in a title.
        "Title": title.encode("ascii", "ignore").decode() or "New note",
        "Tags": tags,
        "Priority": priority,
    }
    if click_url:
        headers["Click"] = click_url
    try:
        httpx.post(
            f"{server}/{topic}",
            content=body.encode("utf-8"),
            headers=headers,
            timeout=10.0,
        ).raise_for_status()
    except Exception:
        # A note that saved but failed to notify is still a saved note.
        log.exception("Failed to push notification to %s/%s", server, topic)
