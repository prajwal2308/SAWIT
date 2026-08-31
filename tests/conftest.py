import pytest

from app import accounts
from app.config import Settings
from app.schemas import KeyFact, ReelNote
from app.store import Store

API_KEY = "test-key-not-a-real-secret"
IG_APP_SECRET = "test-app-secret"
IG_VERIFY_TOKEN = "test-verify-token"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        api_key=API_KEY,
        db_path=str(tmp_path / "notes.sqlite3"),
        llm_backend="anthropic",
        model="claude-opus-5",
        nvidia_api_key=None,
        nvidia_base_url="https://integrate.api.nvidia.example/v1",
        vision=True,
        # Off by default so nothing in the suite reaches the embedding endpoint
        # by accident; the tests that exercise it turn it on and stub the call.
        embed_model="",
        asr_backend="faster-whisper",
        whisper_model="tiny",
        asr_model="whisper-1",
        asr_base_url=None,
        asr_api_key=None,
        ntfy_server="https://ntfy.example",
        ntfy_topic=None,
        public_base_url=None,
        cookies_file=None,
        frame_count=2,
        max_duration_seconds=900,
        ig_app_secret=IG_APP_SECRET,
        ig_verify_token=IG_VERIFY_TOKEN,
        ig_access_token="ig-access-token",
        ig_api_base="https://graph.instagram.example",
        ig_api_version="v23.0",
    )


def make_note(**overrides) -> ReelNote:
    """A representative note — the finance case, which exercises `steps`."""
    data = dict(
        title="The 50/30/20 budget rule",
        category="finance",
        one_liner="Split take-home pay into needs, wants and savings.",
        takeaways=["50% needs", "30% wants", "20% savings"],
        key_facts=[KeyFact(label="Savings share", value="20%")],
        steps=["Start from take-home pay", "Multiply by 0.5 for needs"],
        caveats=["Assumes stable monthly income"],
        tags=["budget", "money"],
    )
    data.update(overrides)
    return ReelNote(**data)


TEST_EMAIL = "owner@test"
TEST_PASSWORD = "correct-horse-battery"


def bound_store(path: str, email: str = TEST_EMAIL, api_key: str = API_KEY) -> Store:
    """A Store bound to a fresh account.

    Notes are per-account now, so a Store has to be told whose notes it is
    looking at before it will answer at all — see tests/test_isolation.py.
    """
    base = Store(path)
    user_id = base.create_user(email, accounts.hash_password(TEST_PASSWORD), api_key)
    if user_id is None:                       # the account already exists
        user_id = base.user_by_email(email)["id"]
    return base.for_user(user_id)


@pytest.fixture
def store(settings) -> Store:
    return bound_store(settings.db_path)
