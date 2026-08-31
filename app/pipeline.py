"""source in, note out. The whole product is this function."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import extract as extract_mod
from . import instagram, notify, transcribe
from . import media as media_mod
from .config import Settings
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Source:
    """Where a reel came from, and where the answer should go back to.

    `media_url` set means Meta handed us the file in a DM webhook — download it
    directly. Otherwise we only have a page URL and have to scrape it.
    """

    page_url: str
    media_url: str | None = None
    reply_to: str | None = None
    title: str | None = None


def process(note_id: str, source: Source, settings: Settings, store: Store) -> None:
    """Run one reel end to end. Never raises: failures are recorded and reported."""
    try:
        with tempfile.TemporaryDirectory(prefix="sawit-") as tmp:
            workdir = Path(tmp)

            if source.media_url:
                item = media_mod.fetch_direct(
                    source.media_url,
                    workdir,
                    frame_count=settings.frame_count,
                    max_duration_seconds=settings.max_duration_seconds,
                    title=source.title,
                )
            else:
                item = media_mod.fetch(
                    source.page_url,
                    workdir,
                    cookies_file=settings.cookies_file,
                    frame_count=settings.frame_count,
                    max_duration_seconds=settings.max_duration_seconds,
                )

            text = ""
            if item.audio_path is not None:
                text = transcribe.transcribe(
                    item.audio_path,
                    backend=settings.asr_backend,
                    model_size=settings.whisper_model,
                    base_url=settings.asr_base_url,
                    api_key=settings.asr_api_key,
                    hosted_model=settings.asr_model,
                )
            note = extract_mod.extract(
                transcript=text,
                # A text-only model 400s on image blocks; the frames are still
                # kept for the thumbnail either way.
                frames=item.frames if settings.vision else [],
                model=settings.model,
                backend=settings.llm_backend,
                base_url=settings.nvidia_base_url,
                api_key=settings.nvidia_api_key,
                source_title=item.title,
                uploader=item.uploader,
                description=item.description,
            )
            # The middle frame is the most representative one; the samples are
            # already taken from inside the clip.
            thumbnail = item.frames[len(item.frames) // 2] if item.frames else None

        store.save_note(
            note_id,
            note,
            transcript=text,
            source_title=item.title,
            uploader=item.uploader,
            duration=item.duration,
            thumbnail=thumbnail,
        )
        _deliver(note_id, source, note, settings)

    except Exception as exc:
        log.exception("Failed to process %s", source.page_url)
        store.mark_failed(note_id, str(exc))
        _deliver_failure(source, str(exc), settings)


def _deliver(note_id: str, source: Source, note, settings: Settings) -> None:
    base = settings.public_base_url
    link = f"{base}/notes/{note_id}" if base else None

    # Answering in the DM thread is the whole point of the Instagram path: the
    # reply lands where the share happened, so nothing else has to be opened.
    if source.reply_to:
        try:
            instagram.send_text(
                source.reply_to, instagram.format_reply(note, link), settings
            )
            return
        except instagram.InstagramError:
            log.exception("Could not reply in the DM thread; falling back to push")

    if settings.push_enabled:
        notify.push_note(
            note, server=settings.ntfy_server, topic=settings.ntfy_topic, click_url=link
        )


def _deliver_failure(source: Source, error: str, settings: Settings) -> None:
    if source.reply_to:
        try:
            instagram.send_text(
                source.reply_to, f"Could not save that one: {error}"[:900], settings
            )
            return
        except instagram.InstagramError:
            log.exception("Could not report the failure in the DM thread")

    if settings.push_enabled:
        notify.push_failure(
            source.page_url, error, server=settings.ntfy_server, topic=settings.ntfy_topic
        )
