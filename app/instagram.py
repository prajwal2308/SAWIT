"""Instagram DM ingestion — the path that never makes you leave the app.

When someone shares a reel to a professional account, Meta's webhook delivers
an `ig_reel` attachment carrying a direct media URL. That is a sanctioned
handoff: no scraping, no cookie jar, and the reply lands back in the same DM
thread, so the whole interaction looks exactly like sending a reel to a friend
who happens to answer with the summary.

Endpoint shapes move between Graph versions. Everything version-specific is an
env var rather than a literal, so a bump is config, not a code change.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

import httpx

from .config import Settings
from .schemas import ReelNote

log = logging.getLogger(__name__)

# Attachment types that carry a playable reel. `share`/`post` may arrive for
# feed posts, which have no video to transcribe, so they are deliberately out.
REEL_ATTACHMENTS = {"ig_reel", "reel", "video"}

# Instagram rejects long DM bodies; leave room under the documented 1000.
MAX_DM_CHARS = 950


class InstagramError(RuntimeError):
    pass


@dataclass
class IncomingReel:
    """One shared reel, normalized out of a webhook payload."""

    sender_id: str
    mid: str
    media_url: str | None = None
    page_url: str | None = None
    title: str | None = None

    @property
    def reference(self) -> str:
        """What we record as the note's source, for the 'open original' link."""
        return self.page_url or self.media_url or f"instagram-dm:{self.mid}"


def verify_subscription(mode: str | None, token: str | None, challenge: str | None,
                        settings: Settings) -> str:
    """Answer Meta's GET handshake when you first subscribe the webhook."""
    if not settings.ig_verify_token:
        raise InstagramError("IG_VERIFY_TOKEN is not set.")
    if mode != "subscribe" or not token or not hmac.compare_digest(
        token, settings.ig_verify_token
    ):
        raise InstagramError("Webhook verification failed.")
    return challenge or ""


def verify_signature(raw_body: bytes, header: str | None, settings: Settings) -> bool:
    """Check X-Hub-Signature-256 so only Meta can queue work on your server."""
    if not settings.ig_app_secret:
        raise InstagramError("IG_APP_SECRET is not set; refusing unverified webhooks.")
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.ig_app_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def parse_events(payload: dict) -> list[IncomingReel]:
    """Pull shared reels out of a webhook body, ignoring everything else."""
    found: list[IncomingReel] = []
    for entry in payload.get("entry") or []:
        for event in entry.get("messaging") or []:
            message = event.get("message")
            if not isinstance(message, dict):
                continue  # delivery receipts, reads, reactions
            if message.get("is_echo"):
                continue  # our own replies coming back — never reprocess these
            sender_id = (event.get("sender") or {}).get("id")
            mid = message.get("mid")
            if not sender_id or not mid:
                continue

            reel = _reel_from_attachments(message, sender_id, mid)
            if reel is None:
                reel = _reel_from_text(message, sender_id, mid)
            if reel is not None:
                found.append(reel)
    return found


def _reel_from_attachments(message: dict, sender_id: str, mid: str) -> IncomingReel | None:
    for attachment in message.get("attachments") or []:
        if attachment.get("type") not in REEL_ATTACHMENTS:
            continue
        payload = attachment.get("payload") or {}
        url = payload.get("url")
        if not url:
            continue
        return IncomingReel(
            sender_id=sender_id,
            mid=mid,
            media_url=url,
            # Not always present; when it is, it is the nicer permalink to keep.
            page_url=payload.get("permalink_url") or payload.get("reel_permalink"),
            title=payload.get("title"),
        )
    return None


def _reel_from_text(message: dict, sender_id: str, mid: str) -> IncomingReel | None:
    """Someone pasted a link into the DM instead of sharing the reel itself."""
    for token in (message.get("text") or "").split():
        if token.startswith(("http://", "https://")):
            return IncomingReel(sender_id=sender_id, mid=mid, page_url=token)
    return None


def send_text(recipient_id: str, text: str, settings: Settings) -> None:
    if not settings.ig_access_token:
        raise InstagramError("IG_ACCESS_TOKEN is not set.")
    url = f"{settings.ig_api_base}/{settings.ig_api_version}/me/messages"
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {settings.ig_access_token}"},
            json={"recipient": {"id": recipient_id}, "message": {"text": text}},
            timeout=15.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise InstagramError(
            f"Instagram rejected the reply ({exc.response.status_code}): "
            f"{exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise InstagramError(f"Could not reach the Instagram send API: {exc}") from exc


def format_reply(note: ReelNote, link: str | None = None) -> str:
    """Plain text — Instagram DMs render no markdown, so bullets are literal."""
    lines = [note.title, "", note.one_liner]
    if note.takeaways:
        lines += ["", *(f"• {t}" for t in note.takeaways)]
    if note.steps:
        lines += ["", "How:", *(f"{i}. {s}" for i, s in enumerate(note.steps, 1))]
    if note.key_facts:
        lines += ["", *(f"{f.label}: {f.value}" for f in note.key_facts)]

    body = "\n".join(lines)
    suffix = f"\n\n{link}" if link else ""
    budget = MAX_DM_CHARS - len(suffix)
    if len(body) > budget:
        body = body[: budget - 1].rstrip() + "…"
    return body + suffix
