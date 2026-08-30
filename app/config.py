"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Everything the service needs to run, resolved from env vars."""

    # Shared secret the iOS Shortcut sends as X-API-Key. The service refuses to
    # start without it: this endpoint downloads and transcribes whatever URL it
    # is handed, so an open one is somebody else's free compute.
    api_key: str
    db_path: str

    model: str
    asr_backend: str
    whisper_model: str

    ntfy_server: str
    ntfy_topic: str | None
    public_base_url: str | None

    # yt-dlp needs a logged-in session for most Instagram/Facebook URLs.
    cookies_file: str | None
    frame_count: int
    max_duration_seconds: int

    # Instagram DM ingestion. Optional: without these the service still works
    # through the iOS Shortcut, it just cannot receive or answer DMs.
    ig_app_secret: str | None
    ig_verify_token: str | None
    ig_access_token: str | None
    ig_api_base: str
    ig_api_version: str

    @property
    def push_enabled(self) -> bool:
        return bool(self.ntfy_topic)

    @property
    def instagram_enabled(self) -> bool:
        return bool(self.ig_app_secret and self.ig_verify_token and self.ig_access_token)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    api_key = os.environ.get("SAWIT_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "SAWIT_API_KEY is not set. Generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(32))'` and set it "
            "on both the server and the iOS Shortcut."
        )
    return Settings(
        api_key=api_key,
        db_path=os.environ.get("SAWIT_DB", "sawit.sqlite3"),
        model=os.environ.get("SAWIT_MODEL", "claude-opus-5"),
        asr_backend=os.environ.get("SAWIT_ASR", "faster-whisper"),
        whisper_model=os.environ.get("SAWIT_WHISPER_MODEL", "small"),
        ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=os.environ.get("NTFY_TOPIC") or None,
        public_base_url=(os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/") or None,
        cookies_file=os.environ.get("YTDLP_COOKIES_FILE") or None,
        frame_count=int(os.environ.get("SAWIT_FRAMES", "4")),
        max_duration_seconds=int(os.environ.get("SAWIT_MAX_DURATION", "900")),
        ig_app_secret=os.environ.get("IG_APP_SECRET") or None,
        ig_verify_token=os.environ.get("IG_VERIFY_TOKEN") or None,
        ig_access_token=os.environ.get("IG_ACCESS_TOKEN") or None,
        ig_api_base=os.environ.get("IG_API_BASE", "https://graph.instagram.com").rstrip("/"),
        ig_api_version=os.environ.get("IG_API_VERSION", "v23.0"),
    )
