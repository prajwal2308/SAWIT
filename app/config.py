"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


def _url_or_none(raw: str | None) -> str | None:
    """Only a real link. Anything else — blank, a placeholder, someone typing
    "disabled" to turn it off — becomes None rather than a broken href, and the
    page that would have shown it falls back to the manual instructions."""
    value = (raw or "").strip()
    return value if value.startswith(("http://", "https://")) else None


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

    # Which LLM does the extraction: "anthropic" or "nvidia". `nvidia` is any
    # OpenAI-compatible endpoint — override the base URL to point it elsewhere.
    llm_backend: str
    model: str
    nvidia_api_key: str | None
    nvidia_base_url: str
    # Send frames to the model. Turn off for a text-only model.
    vision: bool
    # Meaning-based search alongside the keyword index. Empty model disables it,
    # and the service runs exactly as before.
    embed_model: str
    # An iCloud link to the Save Reel shortcut. iOS cannot install a shortcut
    # from a web page, but it can install one from an iCloud link in two taps —
    # which is the difference between people using this and not. Made once from
    # a phone that already has the shortcut: Share -> Copy iCloud Link.
    shortcut_url: str | None

    asr_backend: str
    whisper_model: str
    asr_model: str
    asr_base_url: str | None
    asr_api_key: str | None

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


# Free multimodal endpoint on build.nvidia.com. Reads the on-screen text, which
# a text-only model cannot do — see SAWIT_VISION. This one is chosen for a
# specific reason: it takes up to 12 images per request, and the llama vision
# models take exactly one. A reel puts its numbers on screen across the whole
# clip, so one frame is not enough to read it.
DEFAULT_NVIDIA_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

# Retrieval vectors, from the same OpenAI-compatible endpoint as the extraction,
# so semantic search needs no second key and nothing running locally.
DEFAULT_EMBED_MODEL = "nvidia/nemotron-3-embed-1b"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Running locally you keep these in a .env; in a container they are already
    # in the environment, and a real env var always wins over the file.
    load_dotenv(override=False)

    api_key = os.environ.get("SAWIT_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "SAWIT_API_KEY is not set. Generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(32))'` and set it "
            "on both the server and the iOS Shortcut."
        )
    backend = os.environ.get("SAWIT_LLM", "anthropic").strip().lower()
    if backend not in {"anthropic", "nvidia"}:
        raise RuntimeError(f"SAWIT_LLM must be 'anthropic' or 'nvidia', not {backend!r}.")
    default_model = DEFAULT_NVIDIA_MODEL if backend == "nvidia" else DEFAULT_ANTHROPIC_MODEL

    return Settings(
        api_key=api_key,
        db_path=os.environ.get("SAWIT_DB", "sawit.sqlite3"),
        llm_backend=backend,
        model=os.environ.get("SAWIT_MODEL", default_model),
        nvidia_api_key=os.environ.get("NVIDIA_API_KEY") or None,
        nvidia_base_url=os.environ.get(
            "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
        ).rstrip("/"),
        vision=_flag("SAWIT_VISION", default=True),
        embed_model=os.environ.get("SAWIT_EMBED_MODEL", DEFAULT_EMBED_MODEL).strip(),
        shortcut_url=_url_or_none(os.environ.get("SAWIT_SHORTCUT_URL")),
        asr_backend=os.environ.get("SAWIT_ASR", "faster-whisper"),
        whisper_model=os.environ.get("SAWIT_WHISPER_MODEL", "small"),
        asr_model=os.environ.get("ASR_MODEL", "whisper-1"),
        asr_base_url=(os.environ.get("ASR_BASE_URL") or "").rstrip("/") or None,
        asr_api_key=os.environ.get("ASR_API_KEY") or None,
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
