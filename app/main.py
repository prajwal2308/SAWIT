"""HTTP surface: one write endpoint for the Shortcut, a few read views for you."""

from __future__ import annotations

import html
import logging
import secrets
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any
from urllib.parse import quote_plus, urlencode

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, ValidationError, field_validator

from . import accounts, instagram
from . import embed as embed_mod
from .config import Settings, get_settings
from .pipeline import Source, process
from .store import Store

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Fail fast on missing config rather than at the first shared reel, and
    # clear out anything a restart left mid-flight.
    settings = get_settings()
    bootstrap_owner(base_store(), settings)
    orphans = base_store().recover_orphans()
    if orphans:
        log.warning("Marked %d interrupted note(s) as failed; they can be retried", orphans)
    log.info(
        "Sawit ready — instagram=%s push=%s asr=%s",
        settings.instagram_enabled, settings.push_enabled, settings.asr_backend,
    )
    yield


app = FastAPI(title="Sawit", docs_url=None, redoc_url=None, lifespan=lifespan)


@lru_cache(maxsize=1)
def base_store() -> Store:
    """The unbound store: migrations, accounts, and boot-time recovery.

    It cannot read notes — see Store's docstring. Everything that serves a
    request goes through get_store() instead, which is bound to an account.
    """
    return Store(get_settings().db_path)


def bootstrap_owner(store: Store, settings: Settings) -> None:
    """Turn a single-user deployment into account one, without losing anything.

    SAWIT_API_KEY was the whole authentication story before accounts existed,
    and it is already in somebody's Shortcut. It becomes the first account's
    key, and the notes that predate accounts are handed to it — otherwise an
    upgrade silently empties the library.
    """
    if store.user_count():
        return
    owner = store.create_user(
        email=OWNER_EMAIL,
        password_hash=accounts.hash_password(secrets.token_urlsafe(32)),
        api_key=settings.api_key,
    )
    if owner is None:
        return
    adopted = store.adopt_orphan_notes(owner)
    log.info("Created the first account and gave it %d existing note(s)", adopted)


SESSION_COOKIE = "sawit_session"
OWNER_EMAIL = "owner@localhost"


def current_user(
    request: Request,
    k: str | None = Query(default=None, description="Key for browser views."),
    settings: Settings = Depends(get_settings),
    store: Store = Depends(base_store),
) -> dict[str, Any]:
    """Whose request this is.

    Three ways in, in the order they are cheapest to check: the Shortcut's
    header, a signed session cookie, and ?k= on a first browser visit. All
    three resolve to one account, and the account is what everything
    downstream is scoped to.
    """
    supplied = request.headers.get("x-api-key") or k or request.cookies.get(KEY_COOKIE)
    if supplied and supplied.isascii():
        user = store.user_by_api_key(supplied)
        if user:
            if k and request.cookies.get(KEY_COOKIE) != supplied:
                request.state.grant_cookie = supplied
            return user

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        user_id = accounts.read_session(token, settings.api_key)
        if user_id:
            user = store.user_by_id(user_id)
            if user:
                return user

    raise HTTPException(status_code=401, detail="Bad or missing API key.")


def get_store(
    user: dict[str, Any] = Depends(current_user),
    base: Store = Depends(base_store),
) -> Store:
    """A store that can only see the notes of whoever is asking."""
    return base.for_user(user["id"])


# A key in ?k= is a key in your history, your bookmarks and every referer the
# page sends. One visit with ?k= trades it for this cookie, and the links the
# pages generate go clean from the next request on.
KEY_COOKIE = "sawit_key"
KEY_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def require_key(user: dict[str, Any] = Depends(current_user)) -> None:
    """The gate every protected route already declares. Resolving the account
    is the check now — an unknown key matches no account."""
    return None


@app.middleware("http")
async def _persist_key_cookie(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    granted = getattr(request.state, "grant_cookie", None)
    if granted:
        response.set_cookie(
            KEY_COOKIE, granted,
            max_age=KEY_COOKIE_MAX_AGE, httponly=True, samesite="lax",
            # Over plain http — a laptop on localhost — a Secure cookie is
            # simply dropped, and the browser views stop working.
            secure=request.url.scheme == "https",
        )
    return response


def _link_key(request: Request) -> str:
    """The key to thread through generated links: nothing, once a cookie holds it.

    Still emitted on the visit that sets the cookie, so a browser that refuses
    cookies keeps working on ?k= alone rather than locking itself out.
    """
    if request.cookies.get(KEY_COOKIE):
        return ""
    return getattr(request.state, "grant_cookie", "") or ""


def _qs(**params: str | None) -> str:
    """A query string with the empty values dropped, so no bare `?k=` survives."""
    kept = {p: v for p, v in params.items() if v}
    return f"?{urlencode(kept)}" if kept else ""


class IngestRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _must_be_http(cls, value: str) -> str:
        value = value.strip()
        # The share sheet sometimes hands over "caption text <url>"; take the URL.
        for token in value.split():
            if token.startswith(("http://", "https://")):
                return token
        raise ValueError("No http(s) URL found in the shared text.")


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> JSONResponse:
    """Installs Sawit to the home screen — and, on Android, into the share sheet.

    share_target is why this file exists. Chrome registers an installed PWA as a
    system share target, so sharing a reel offers Sawit directly: no Shortcut to
    build, no key to paste, nothing to configure. Safari does not implement it,
    which is why iOS still needs the Shortcut.
    """
    return JSONResponse(
        {
            "name": "Sawit",
            "short_name": "Sawit",
            "description": "Reels, in words you can search.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#000000",
            "theme_color": "#000000",
            "icons": [
                {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml",
                 "purpose": "any maskable"},
            ],
            "share_target": {
                "action": "/share",
                "method": "GET",
                "params": {"title": "title", "text": "text", "url": "url"},
            },
        },
        media_type="application/manifest+json",
    )


@app.get("/icon.svg", include_in_schema=False)
def icon() -> Response:
    """One glyph, no binaries in the repo: a bookmark, which is the whole idea."""
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'>"
        "<rect width='512' height='512' rx='114' fill='#0a84ff'/>"
        "<path d='M176 118h160a26 26 0 0 1 26 26v250a12 12 0 0 1-19 10l-83-60a12 12 0 0 0-14 0"
        "l-83 60a12 12 0 0 1-19-10V144a26 26 0 0 1 26-26z' fill='#fff'/>"
        "</svg>"
    )
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/share", dependencies=[Depends(require_key)])
def share_target(
    request: Request,
    background: BackgroundTasks,
    url: str | None = None,
    text: str | None = None,
    title: str | None = None,
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> Response:
    """Where Android's share sheet lands.

    Which field holds the link is up to the sharing app: Instagram tends to put
    it in `text`, others use `url`, and some bury it in a sentence — so try them
    all and let IngestRequest pull the URL out of whatever arrives.
    """
    for candidate in (url, text, title):
        if not candidate:
            continue
        try:
            link = IngestRequest(url=candidate).url
        except ValidationError:
            continue
        existing = store.find_written(link)
        if existing:
            return _redirect(f"/notes/{existing}")
        note_id = store.create_pending(link)
        background.add_task(process, note_id, Source(page_url=link), settings, store)
        return _redirect(f"/notes/{note_id}")
    return _redirect("/?error=There+was+no+link+in+what+you+shared.")


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str | None = None, joined: str | None = None) -> HTMLResponse:
    return HTMLResponse(_auth_page("in", error, joined))


@app.get("/signup", response_class=HTMLResponse)
def signup_page(error: str | None = None) -> HTMLResponse:
    return HTMLResponse(_auth_page("up", error, None))


@app.post("/login")
def login(
    email: str = Form(...),
    password: str = Form(...),
    settings: Settings = Depends(get_settings),
    store: Store = Depends(base_store),
) -> Response:
    user = store.user_by_email(accounts.normalise_email(email))
    # Verify against a decoy hash when the account is unknown, so a wrong email
    # and a wrong password take the same time to fail and cannot be told apart.
    stored = user["password_hash"] if user else _DECOY_HASH
    if not accounts.verify_password(password, stored) or user is None:
        return _redirect("/login?error=Those+details+did+not+match.")
    return _with_session(_redirect("/"), user["id"], settings)


@app.post("/signup")
def signup(
    email: str = Form(...),
    password: str = Form(...),
    settings: Settings = Depends(get_settings),
    store: Store = Depends(base_store),
) -> Response:
    email = accounts.normalise_email(email)
    if "@" not in email or len(email) < 3:
        return _redirect("/signup?error=That+is+not+an+email+address.")
    try:
        accounts.check_password_strength(password)
    except accounts.AuthError as exc:
        return _redirect(f"/signup?error={quote_plus(str(exc))}")

    user_id = store.create_user(email, accounts.hash_password(password),
                                accounts.new_api_key())
    if user_id is None:
        return _redirect("/signup?error=That+email+is+already+registered.")
    return _with_session(_redirect("/"), user_id, settings)


@app.post("/logout")
def logout() -> Response:
    response = _redirect("/login")
    # Both, or the API key cookie would silently sign you back in.
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(KEY_COOKIE)
    return response


@app.get("/account", response_class=HTMLResponse, dependencies=[Depends(require_key)])
def account_page(
    error: str | None = None,
    saved: str | None = None,
    user: dict[str, Any] = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """The key the Shortcut sends, and a way in that is not a URL with a key in it."""
    unclaimed = user["email"] == OWNER_EMAIL
    whoami = "No sign-in set yet" if unclaimed else html.escape(user["email"])
    banner = f"<p class=warn>{html.escape(error)}</p>" if error else ""
    if saved:
        banner += "<p class=good>Saved. You can sign in with that from now on.</p>"

    claim = (
        "<h2>" + ("Set a password" if unclaimed else "Change your sign-in") + "</h2>"
        + ("<p class=lede>This account holds your notes but has no password yet, so "
           "the only way in is a link with the key in it. Give it an email and "
           "password and you can sign in normally.</p>" if unclaimed else "")
        + "<form method=post action='/account/credentials' class=auth-form>"
        f"<input name=email type=email required placeholder='Email' autocomplete=email"
        f" autocapitalize=off autocorrect=off spellcheck=false"
        f" value=\"{'' if unclaimed else html.escape(user['email'], quote=True)}\">"
        "<input name=password type=password required placeholder='New password'"
        " autocomplete=new-password>"
        "<button>Save</button></form>"
    )

    return HTMLResponse(_page(
        "<a class=back href='/'>&lsaquo; All notes</a>"
        f"<h1>Account</h1>"
        f"<p class=note-meta>{whoami}</p>"
        f"{banner}"
        "<h2>Your key</h2>"
        "<p class=lede>The Shortcut sends this as <code>X-API-Key</code>. "
        "It opens your notes and nobody else's — treat it like a password.</p>"
        f"<pre class=keybox>{html.escape(user['api_key'])}</pre>"
        f"{_setup_help(settings, user['api_key'])}"
        f"{claim}"
        "<form method=post action='/logout' class=actions>"
        "<button class=danger>Sign out</button></form>"
    ))


def _setup_help(settings: Settings, api_key: str) -> str:
    """Getting a reel in, in the order it actually happens.

    Building the Shortcut by hand is fifteen steps and three places to go wrong,
    and it is where people give up. iOS will not let a web page install one, but
    it will install one from an iCloud link in two taps — so when a deployment
    has that link, it leads.
    """
    key = html.escape(api_key, quote=True)
    if settings.shortcut_url:
        install = (
            f"<a class=row href='{html.escape(settings.shortcut_url, quote=True)}'>"
            f"<span>Add the Save Reel shortcut</span><span class=chev>&rsaquo;</span></a>"
            "<p class=lede>Two taps: <b>Add Shortcut</b>, then open it once and put "
            "your key in the <code>X-API-Key</code> header — it arrives with a "
            "placeholder, because a shortcut anyone can install cannot carry "
            "somebody else's key.</p>"
        )
    else:
        install = (
            "<p class=lede>No one-tap installer is set up for this deployment yet. "
            "Build the Shortcut once by hand, then <b>Share \u2192 Copy iCloud Link</b> "
            "and set that link as <code>SAWIT_SHORTCUT_URL</code> — after that "
            "everyone else installs it in two taps instead of fifteen steps.</p>"
        )

    return (
        "<h2>Saving reels from your iPhone</h2>"
        + install
        + "<details class=row-d><summary><span>Or build it by hand</span>"
        "<span class=chev>&rsaquo;</span></summary><p>"
        "In <b>Shortcuts</b>: a new shortcut called <i>Save Reel</i> with one "
        "<b>Get Contents of URL</b> action. Method <b>POST</b>. Headers "
        f"<code>X-API-Key: {key}</code> and <code>Content-Type: application/json</code>. "
        "Request body <b>JSON</b>, one Text field named <code>url</code>, and its "
        "value must be the blue <b>Shortcut Input</b> variable rather than typed text. "
        "Then \u2139\ufe0f \u2192 <b>Show in Share Sheet</b>, types <b>URLs</b> and "
        "<b>Text</b> only, and no other action \u2014 no \u201cShow Result\u201d is "
        "what makes the sheet close instantly."
        "<br><br><b>It will not appear until you enable it once:</b> share anything, "
        "scroll to the bottom of the list, <b>Edit Actions</b>, switch <i>Save Reel</i> "
        "on, and tap the green + to pin it where you can reach it."
        "</p></details>"
        "<details class=row-d><summary><span>Or skip it entirely</span>"
        "<span class=chev>&rsaquo;</span></summary><p>"
        "Add this page to your home screen and use the <b>Add</b> tab. Copy a reel "
        "link in Instagram, paste, Save. Two more taps than the share sheet, and "
        "nothing to set up at all."
        "</p></details>"
    )


@app.post("/account/credentials", dependencies=[Depends(require_key)])
def set_credentials(
    email: str = Form(...),
    password: str = Form(...),
    user: dict[str, Any] = Depends(current_user),
    settings: Settings = Depends(get_settings),
    store: Store = Depends(base_store),
) -> Response:
    """Claim the account you are already authenticated as.

    The account bootstrapped from SAWIT_API_KEY holds the notes but has no
    password, so this is how its owner gets a way in that is not a URL with a
    key in it. Signing up fresh instead would leave the library behind.
    """
    email = accounts.normalise_email(email)
    if "@" not in email:
        return _redirect("/account?error=That+is+not+an+email+address.")
    try:
        accounts.check_password_strength(password)
    except accounts.AuthError as exc:
        return _redirect(f"/account?error={quote_plus(str(exc))}")
    if not store.set_credentials(user["id"], email, accounts.hash_password(password)):
        return _redirect("/account?error=That+email+belongs+to+another+account.")
    return _with_session(_redirect("/account?saved=1"), user["id"], settings)


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _with_session(response: Response, user_id: str, settings: Settings) -> Response:
    response.set_cookie(
        SESSION_COOKIE, accounts.sign_session(user_id, settings.api_key),
        max_age=accounts.SESSION_TTL, httponly=True, samesite="lax",
        secure=settings.public_base_url is not None
        and settings.public_base_url.startswith("https"),
    )
    return response


# Hashing a throwaway password once at import keeps the unknown-account path
# the same cost as the known one.
_DECOY_HASH = accounts.hash_password(secrets.token_urlsafe(16))


def _auth_page(mode: str, error: str | None, joined: str | None) -> str:
    signing_in = mode == "in"
    action, verb = ("/login", "Sign in") if signing_in else ("/signup", "Create account")
    other = ("<p class=swap>New here? <a href='/signup'>Create an account</a></p>"
             if signing_in else
             "<p class=swap>Already have one? <a href='/login'>Sign in</a></p>")
    banner = f"<p class=warn>{html.escape(error)}</p>" if error else ""
    if joined:
        banner += "<p class=good>Account created. Sign in to continue.</p>"
    return _page(
        f"<div class=auth><h1>Sawit</h1>"
        f"<p class=tag>Reels, in words you can search.</p>{banner}"
        f"<form method=post action='{action}'>"
        f"<input name=email type=email required placeholder='Email' autocomplete=email"
        f" autocapitalize=off autocorrect=off spellcheck=false>"
        f"<input name=password type=password required placeholder='Password'"
        f" autocomplete='{'current' if signing_in else 'new'}-password'>"
        f"<button>{verb}</button></form>{other}</div>"
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Unauthenticated and deliberately dull — this is the platform's probe."""
    return {"status": "ok"}


@app.get("/api/status", dependencies=[Depends(require_key)])
def status(
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """What is actually wired up. The first thing to check on a fresh deploy."""
    return {
        "notes": store.status_counts(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "asr_backend": settings.asr_backend,
        "llm_backend": settings.llm_backend,
        "model": settings.model,
        "vision": settings.vision,
        "llm_key_set": bool(
            settings.nvidia_api_key if settings.llm_backend == "nvidia" else True
        ),
        "instagram_dm": settings.instagram_enabled,
        "push": settings.push_enabled,
        "cookies_file": bool(settings.cookies_file),
        "public_base_url": settings.public_base_url,
    }


@app.post("/ingest", status_code=202, dependencies=[Depends(require_key)])
def ingest(
    payload: IngestRequest,
    background: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> JSONResponse:
    """Accept and return immediately — the share sheet must never wait on us."""
    existing = store.find_written(payload.url)
    if existing:
        return JSONResponse({"id": existing, "status": "ready", "duplicate": True})
    note_id = store.create_pending(payload.url)
    background.add_task(process, note_id, Source(page_url=payload.url), settings, store)
    return JSONResponse({"id": note_id, "status": "pending"}, status_code=202)


@app.post("/add", dependencies=[Depends(require_key)])
def add_from_page(
    request: Request,
    background: BackgroundTasks,
    url: str = Form(...),
    k: str | None = None,
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> Response:
    """Paste a link straight into the page — no Shortcut, no phone, no Meta app."""
    try:
        url = IngestRequest(url=url).url
    except ValidationError:
        raise HTTPException(status_code=400, detail="That is not a link.") from None
    existing = store.find_written(url)
    if existing:
        # Re-pasting a link you already saved takes you to the note, rather
        # than making a second copy of it.
        return _redirect_or_json(request, k, f"/notes/{existing}",
                                 {"id": existing, "status": "ready", "duplicate": True})
    note_id = store.create_pending(url)
    background.add_task(process, note_id, Source(page_url=url), settings, store)
    return _redirect_or_json(request, k, f"/notes/{note_id}",
                             {"id": note_id, "status": "pending"})


@app.post("/api/reindex", dependencies=[Depends(require_key)])
def reindex(
    limit: int = 200,
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """Embed notes written before this existed, or whose embedding failed.

    Safe to run repeatedly: it only touches notes that have no vector, so a
    second call after a partial run picks up exactly what is left.
    """
    pending = store.awaiting_embedding(limit)
    done = sum(1 for note in pending if embed_mod.embed_note(note, settings, store))
    return {"considered": len(pending), "embedded": done,
            "remaining": len(store.awaiting_embedding(limit))}


@app.get("/webhook/instagram", response_class=PlainTextResponse)
def webhook_verify(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> PlainTextResponse:
    """Meta's one-time subscription handshake."""
    params = request.query_params
    try:
        challenge = instagram.verify_subscription(
            params.get("hub.mode"),
            params.get("hub.verify_token"),
            params.get("hub.challenge"),
            settings,
        )
    except instagram.InstagramError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return PlainTextResponse(challenge)


@app.post("/webhook/instagram")
async def webhook_receive(
    request: Request,
    background: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> JSONResponse:
    """Someone shared a reel to the account. Ack fast, work afterwards.

    Meta retries anything that is slow or non-200, so this must never wait on
    the pipeline — and every accepted message is deduplicated by its mid.
    """
    raw = await request.body()
    if not instagram.verify_signature(raw, request.headers.get("x-hub-signature-256"), settings):
        raise HTTPException(status_code=403, detail="Bad webhook signature.")

    accepted = 0
    for reel in instagram.parse_events(await request.json()):
        note_id = store.create_pending(reel.reference, mid=reel.mid)
        if note_id is None:
            continue  # already handled this share on an earlier delivery
        background.add_task(
            process,
            note_id,
            Source(
                page_url=reel.reference,
                media_url=reel.media_url,
                reply_to=reel.sender_id,
                title=reel.title,
            ),
            settings,
            store,
        )
        accepted += 1

    return JSONResponse({"accepted": accepted})


@app.get("/api/notes", dependencies=[Depends(require_key)])
def api_notes(
    q: str | None = None,
    category: str | None = None,
    limit: int = 50,
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> list[dict[str, Any]]:
    if q:
        # The same hybrid the page uses; a JSON caller should not get worse
        # results than the browser for the same query.
        return _hybrid(q, category, settings, store)[:limit]
    return store.recent(limit, category=category)


@app.get("/api/categories", dependencies=[Depends(require_key)])
def api_categories(store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return [{"category": name, "count": count} for name, count in store.category_counts()]


@app.get("/api/notes/{note_id}", dependencies=[Depends(require_key)])
def api_note(note_id: str, store: Store = Depends(get_store)) -> dict[str, Any]:
    note = store.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="No such note.")
    return note


@app.post("/notes/{note_id}/retry", dependencies=[Depends(require_key)])
def retry(
    note_id: str,
    background: BackgroundTasks,
    request: Request,
    k: str | None = None,
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> Response:
    note = store.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="No such note.")
    if not note["url"].startswith(("http://", "https://")):
        # A DM note whose media URL Meta already expired, and no permalink came
        # with it. There is nothing left to fetch — the reel has to be re-shared.
        raise HTTPException(
            status_code=400,
            detail="That reel arrived as a DM attachment and its media link has expired. "
                   "Share it again to reprocess it.",
        )

    store.reset_to_pending(note_id)
    # No reply target on a retry: the DM window has usually closed by now, so
    # the result comes back by push instead.
    background.add_task(process, note_id, Source(page_url=note["url"]), settings, store)
    return _redirect_or_json(
        request, k, f"/notes/{note_id}", {"id": note_id, "status": "pending"}
    )


@app.post("/notes/{note_id}/delete", dependencies=[Depends(require_key)])
def delete_note_form(
    note_id: str,
    request: Request,
    k: str | None = None,
    store: Store = Depends(get_store),
) -> Response:
    if not store.delete(note_id):
        raise HTTPException(status_code=404, detail="No such note.")
    return _redirect_or_json(request, k, "/", {"deleted": note_id})


@app.delete("/api/notes/{note_id}", dependencies=[Depends(require_key)])
def delete_note(note_id: str, store: Store = Depends(get_store)) -> dict[str, str]:
    if not store.delete(note_id):
        raise HTTPException(status_code=404, detail="No such note.")
    return {"deleted": note_id}


def _redirect_or_json(
    request: Request, k: str | None, path: str, payload: dict
) -> Response:
    """Browsers came from a form and want a page; the API wants JSON."""
    if k or request.cookies.get(KEY_COOKIE):
        return RedirectResponse(f"{path}{_qs(k=_link_key(request))}", status_code=303)
    return JSONResponse(payload)


@app.get("/notes/{note_id}/thumb.jpg", dependencies=[Depends(require_key)])
def thumbnail(note_id: str, store: Store = Depends(get_store)) -> Response:
    blob = store.get_thumbnail(note_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="No thumbnail.")
    return Response(content=blob, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_key)])
def index(
    request: Request,
    q: str | None = None,
    category: str | None = None,
    k: str | None = None,
    mode: str | None = None,
    settings: Settings = Depends(get_settings),
    store: Store = Depends(get_store),
) -> HTMLResponse:
    if q:
        notes = _hybrid(q, category, settings, store)
    else:
        notes = store.recent(100, category=category)

    link_key = _link_key(request)
    key = html.escape(link_key, quote=True)
    chips = _category_chips(store.category_counts(), category, link_key, q)
    ready = [n for n in notes if n["status"] == "ready"]
    browse = (
        f"<a class=browse href='/feed{_qs(k=link_key, category=category)}'>"
        f"<span>Flick through {len(ready)} note{'' if len(ready) == 1 else 's'}</span>"
        f"<span class=chev>&rsaquo;</span></a>"
    ) if ready else ""

    if notes:
        rows = ("<div class=grid>"
                + "\n".join(_card(note, key) for note in notes)
                + "</div>")
    elif category:
        rows = f"<p class=empty>Nothing in {html.escape(category)} yet.</p>"
    else:
        rows = "<p class=empty>Nothing saved yet.</p>"

    # Omitted entirely once the cookie carries it: a hidden field with an empty
    # value still puts a bare `k=` on every search you run.
    key_field = f'<input type=hidden name=k value="{key}">' if key else ""
    esc_q = html.escape(q or "", quote=True)
    # Clearing the box has to clear the whole filter. The category used to ride
    # along in a hidden field, so emptying the search you could see left you
    # still filtered by something you could not.
    reset = (f"<a class=clear href='/{_qs(k=link_key)}' aria-label='Clear search'>&times;</a>"
             if (q or category) else "")

    if mode == "search":
        drawer = (
            f"<form method=get class='drawer find'>{key_field}"
            f"<span class=mag aria-hidden=true></span>"
            f"<input name=q value=\"{esc_q}\" type=search autofocus enterkeyhint=search"
            f" placeholder='Search everything you saved' autocomplete=off>"
            f"{reset}</form>"
        )
    elif mode == "add":
        drawer = (
            f"<form method=post class='drawer add' "
            f"action=\"/add{html.escape(_qs(k=link_key), quote=True)}\">"
            f"<input name=url type=url required autofocus placeholder='Paste a reel link'"
            f" autocomplete=off autocapitalize=off autocorrect=off spellcheck=false>"
            f"<button>Save</button></form>"
        )
    else:
        drawer = ""

    return HTMLResponse(_page(
        f"""<header class=top><span class=wordmark>Sawit</span></header>
            {_searching_note(q, category, link_key)}
            {chips}
            {browse}
            {rows}
            {drawer}
            {_tabs(link_key, mode, q, category)}"""
    ))


def _hybrid(
    q: str, category: str | None, settings: Settings, store: Store
) -> list[dict[str, Any]]:
    """Keyword hits first, then anything the meaning search adds.

    The two answer different questions and neither replaces the other. Typing an
    exact phrase you remember should return that note at the top, which is what
    FTS is good at; asking for "budgeting advice" should still surface the note
    called "Allocate monthly net income using a 55/5/10/15/15 split", which
    shares not one word with the query. Keeping FTS first means adding meaning
    never costs precision on the searches that already worked.
    """
    keyword = store.search(q, 100, category=category)
    seen = {n["id"] for n in keyword}
    extra_ids = [i for i in embed_mod.rank(q, settings, store, category=category)
                 if i not in seen]
    return keyword + store.by_ids(extra_ids)


def _searching_note(q: str | None, category: str | None, key: str) -> str:
    """Say what is being filtered, since the box that did it may be closed."""
    if not q and not category:
        return ""
    bits = []
    if q:
        bits.append(f"&ldquo;{html.escape(q)}&rdquo;")
    if category:
        bits.append(html.escape(category))
    return (f"<div class=filtered><span>Showing {' in '.join(bits)}</span>"
            f"<a href='/{_qs(k=key)}'>Clear</a></div>")


def _tabs(key: str, mode: str | None, q: str | None, category: str | None) -> str:
    """The controls live where the thumb is, not stacked above the content."""
    grid_i = ("<svg viewBox='0 0 24 24' aria-hidden=true><rect x='3' y='3' width='7.5' "
              "height='7.5' rx='2'/><rect x='13.5' y='3' width='7.5' height='7.5' rx='2'/>"
              "<rect x='3' y='13.5' width='7.5' height='7.5' rx='2'/>"
              "<rect x='13.5' y='13.5' width='7.5' height='7.5' rx='2'/></svg>")
    feed_i = ("<svg viewBox='0 0 24 24' aria-hidden=true><rect x='3' y='3' width='18' "
              "height='18' rx='4.5'/><path d='M10 8.5l6 3.5-6 3.5z'/></svg>")
    find_i = ("<svg viewBox='0 0 24 24' aria-hidden=true><circle cx='11' cy='11' r='7'/>"
              "<path d='M16.5 16.5L21 21'/></svg>")
    add_i = ("<svg viewBox='0 0 24 24' aria-hidden=true><rect x='3' y='3' width='18' "
             "height='18' rx='5'/><path d='M12 8v8M8 12h8'/></svg>")
    me_i = ("<svg viewBox='0 0 24 24' aria-hidden=true><circle cx='12' cy='8.5' r='4'/>"
            "<path d='M4.5 20.5a7.5 7.5 0 0 1 15 0'/></svg>")

    def tab(href: str, icon: str, label: str, on: bool) -> str:
        return (f"<a class='tab{' on' if on else ''}' href='{html.escape(href, quote=True)}'>"
                f"{icon}<span>{label}</span></a>")

    keeping = _qs(k=key, q=q, category=category)
    return (
        "<nav class=tabs>"
        + tab(f"/{_qs(k=key)}", grid_i, "Notes", mode is None and not q)
        + tab(f"/feed{_qs(k=key, category=category)}", feed_i, "Feed", False)
        + tab(f"/{keeping}{'&' if keeping else '?'}mode=search", find_i, "Search",
              mode == "search")
        + tab(f"/{_qs(k=key)}{'?' if not key else '&'}mode=add", add_i, "Add", mode == "add")
        + tab(f"/account{_qs(k=key)}", me_i, "Account", False)
        + "</nav>"
    )


def _category_chips(
    counts: list[tuple[str, int]], active: str | None, key: str | None, q: str | None
) -> str:
    """One tap to narrow to a topic, without losing the current search."""
    if not counts:
        return ""

    def chip(label: str, value: str | None, count: int) -> str:
        # escape() the query string too: a bare & in an href is invalid HTML.
        href = html.escape(f"/{_qs(k=key, q=q, category=value)}", quote=True)
        css = "chip on" if value == active else "chip"
        return (f"<a class='{css}' href='{href}'>"
                f"{html.escape(label)} <span>{count}</span></a>")

    total = sum(count for _, count in counts)
    chips = [chip("All", None, total)]
    chips += [chip(name, name, count) for name, count in counts]
    return f"<nav class=chips>{''.join(chips)}</nav>"


@app.get("/feed", response_class=HTMLResponse, dependencies=[Depends(require_key)])
def feed(
    request: Request,
    category: str | None = None,
    k: str | None = None,
    store: Store = Depends(get_store),
) -> HTMLResponse:
    """The notes in the shape the reels arrived in: one per screen, thumbed through.

    Scroll-snap does the paging, so the momentum, the rubber-band at the ends and
    the ability to catch a card mid-flight are the platform's rather than ours.
    """
    link_key = _link_key(request)
    qs = html.escape(_qs(k=link_key), quote=True)
    notes = [n for n in store.recent(60, category=category) if n["status"] == "ready"]
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731

    if not notes:
        return HTMLResponse(_page(
            f"<a class=back href='/{_qs(k=link_key)}'>&lsaquo; All notes</a>"
            "<p class=empty>Nothing to flick through yet.</p>"
        ))

    cards: list[str] = []
    sheets: list[str] = []
    for n in notes:
        note_id = esc(n["id"])
        src = f"/notes/{note_id}/thumb.jpg{qs}"
        # The poster is portrait; a wide viewport must letterbox it against a
        # blurred copy of itself rather than cropping into somebody's face.
        thumb = (f"<img class=blur src='{src}' alt='' aria-hidden=true loading=lazy>"
                 f"<img class=poster src='{src}' alt='' loading=lazy>"
                 if n["has_thumbnail"] else "")

        def bullets(items: list, render) -> str:
            return "".join(render(i) for i in items) if items else ""

        facts = ("<div class=facts>" + bullets(
            n["key_facts"],
            lambda f: f"<div class=fact><dt>{esc(f['label'])}</dt>"
                      f"<dd>{esc(f['value'])}</dd></div>",
        ) + "</div>") if n["key_facts"] else ""
        head = (f"<p class=meta>{esc(n['category'])}"
                + (f" &middot; {esc(n['uploader'])}" if n.get("uploader") else "") + "</p>"
                f"<h2 class=feed-title>{esc(n['title'])}</h2>")
        deep = "".join([
            f"<ul class=takeaways>{bullets(n['takeaways'], lambda t: f'<li>{esc(t)}</li>')}</ul>"
            if n["takeaways"] else "",
            f"<ol class=steps>{bullets(n['steps'], lambda s: f'<li>{esc(s)}</li>')}</ol>"
            if n["steps"] else "",
            f"<h3 class=sub>Worth knowing</h3>"
            f"<ul class=takeaways>{bullets(n['caveats'], lambda c: f'<li>{esc(c)}</li>')}</ul>"
            if n["caveats"] else "",
        ])
        # The card is exactly one screen and never scrolls. Whatever does not fit
        # is one tap away, rather than turning the feed into a long document.
        cards.append(
            f"<article class=reel id='card-{note_id}'>"
            # The title already leads the sheet below; painting it over the still
            # as well collided with the reel's own on-screen text.
            f"<a class=stage href='{esc(n['url'])}' target=_blank rel=noopener>"
            f"{thumb}"
            f"<span class=play aria-hidden=true></span>"
            f"<span class=watch>Watch on Instagram</span></a>"
            f"<div class=sheet>{head}"
            f"<p class=lede>{esc(n['one_liner'])}</p>{facts}{deep}"
            f"<a class=row href='/notes/{note_id}{qs}'>"
            f"<span>Open the full note</span><span class=chev>&rsaquo;</span></a>"
            f"</div></article>"
        )

    return HTMLResponse(_page(
        f"<div class=feed>"
        f"<a class='back feed-back' href='/{_qs(k=link_key)}'>&lsaquo; All notes</a>"
        + "".join(cards) + "</div>" + "".join(sheets),
        full_bleed=True,
    ))


@app.get("/notes/{note_id}", response_class=HTMLResponse, dependencies=[Depends(require_key)])
def note_page(
    note_id: str,
    request: Request,
    k: str | None = None,
    store: Store = Depends(get_store),
) -> HTMLResponse:
    note = store.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="No such note.")
    link_key = _link_key(request)
    key = html.escape(link_key, quote=True)
    home = html.escape(f"/{_qs(k=link_key)}", quote=True)
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731

    if note["status"] != "ready":
        failed = note["status"] == "failed"
        detail = esc(note.get("error") or "This one is still being written.")
        return HTMLResponse(_page(
            f"<a class=back href='{home}'>&lsaquo; All notes</a>"
            f"<p style='margin:.6rem 0 .5rem'>"
            f"<span class='pill {'failed' if failed else 'pending'}'>"
            f"<span class=dot></span>{'Failed' if failed else 'Working'}</span></p>"
            f"<h1>{'It did not go through' if failed else 'Working on it'}</h1>"
            f"<p class=lede-lg>{detail}</p>"
            f"<p class=note-meta><a href='{esc(note['url'])}'>The original reel</a></p>"
            f"{_actions(note, key)}"
        ))

    def section(heading: str, items: list, render) -> str:
        if not items:
            return ""
        body = "".join(render(i) for i in items)
        return f"<h2>{heading}</h2><ul>{body}</ul>"

    category_href = html.escape(
        f"/{_qs(k=link_key, category=note['category'])}", quote=True
    )
    parts = [
        f"<a class=back href='{home}'>&lsaquo; All notes</a>",
        f"<h1>{esc(note['title'])}</h1>",
        f"<p class=note-meta><a href='{category_href}'>"
        f"{esc(note['category'])}</a>"
        + (f" &middot; {esc(note['uploader'])}" if note.get("uploader") else "")
        + "</p>",
        f"<p class=lede-lg>{esc(note['one_liner'])}</p>",
        section("Takeaways", note["takeaways"], lambda t: f"<li>{esc(t)}</li>"),
        section("Steps", note["steps"], lambda s: f"<li>{esc(s)}</li>"),
        section("Key facts", note["key_facts"],
                lambda f: f"<li><b>{esc(f['label'])}:</b> {esc(f['value'])}</li>"),
        section("Caveats", note["caveats"], lambda c: f"<li>{esc(c)}</li>"),
        f"<a class=row href='{esc(note['url'])}' target=_blank rel=noopener>"
        f"<span>Open the original reel</span><span class=chev>&rsaquo;</span></a>",
        f"<details class=row-d><summary><span>Transcript</span>"
        f"<span class=chev>&rsaquo;</span></summary>"
        f"<p>{esc(note['transcript'])}</p></details>"
        if note["transcript"] else "",
        _actions(note, key),
    ]
    return HTMLResponse(_page("".join(parts)))


def _actions(note: dict[str, Any], key: str) -> str:
    """Retry and delete. Forms, not links — a link that deletes is a trap for
    anything that prefetches."""
    note_id = html.escape(note["id"])
    qs = html.escape(_qs(k=key), quote=True)
    buttons = ""
    if note["status"] == "failed":
        buttons += (f"<form method=post action='/notes/{note_id}/retry{qs}'>"
                    f"<button>Retry</button></form>")
    buttons += (f"<form method=post action='/notes/{note_id}/delete{qs}' "
                f"onsubmit=\"return confirm('Delete this note?')\">"
                f"<button class=danger>Delete</button></form>")
    return f"<div class=actions>{buttons}</div>"


def _card(note: dict[str, Any], key: str) -> str:
    esc = html.escape
    note_id = esc(note["id"])
    qs = esc(_qs(k=key), quote=True)
    if note["status"] != "ready":
        failed = note["status"] == "failed"
        # State carries a shape and a colour, so what needs you reads at a glance
        # rather than having to be read.
        pill = (f"<span class='pill {'failed' if failed else 'pending'}'>"
                f"<span class=dot></span>{'Failed' if failed else 'Working'}</span>")
        return (f"<a class='tile bare' href='/notes/{note_id}{qs}'>"
                f"<span class=bare-in>{pill}"
                f"<span class=bare-msg>{esc(note.get('error') or note['url'])}</span>"
                f"</span></a>")
    thumb = (f"<img src='/notes/{note_id}/thumb.jpg{qs}' alt='' loading=lazy>"
             if note["has_thumbnail"] else "<span class=noshot></span>")
    return (f"<a class=tile href='/notes/{note_id}{qs}'>{thumb}"
            f"<span class=tile-text>"
            f"<span class=tile-cat>{esc(note['category'])}</span>"
            f"<span class=tile-title>{esc(note['title'])}</span></span></a>")


def _page(body: str, full_bleed: bool = False) -> str:
    cls = "bleed" if full_bleed else ""
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#fbfbfd" media="(prefers-color-scheme:light)">
<meta name=theme-color content="#000000" media="(prefers-color-scheme:dark)">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-title content=Sawit>
<link rel=manifest href=/manifest.webmanifest>
<link rel=apple-touch-icon href=/icon.svg>
<title>Sawit</title><style>
:root{{
  color-scheme:light dark;
  --bg:#fbfbfd; --surface:#fff; --fg:#1d1d1f; --dim:#6e6e73; --faint:#8e8e93;
  --line:rgba(0,0,0,.10); --line-strong:rgba(0,0,0,.16);
  --tint:#0071e3;            /* interactive */
  --tint-soft:rgba(0,113,227,.12);
  --pending:#8e6d00;         /* semantic: still working */
  --pending-bg:rgba(255,196,0,.14);
  --failed:#c7362b;          /* semantic: needs you */
  --failed-bg:rgba(199,54,43,.10);
  --chrome:rgba(251,251,253,.72);
  --press:rgba(0,0,0,.05);
  --shadow:0 1px 2px rgba(0,0,0,.05),0 8px 24px rgba(0,0,0,.06);
}}
@media(prefers-color-scheme:dark){{:root{{
  --bg:#000; --surface:#1c1c1e; --fg:#f5f5f7; --dim:#98989d; --faint:#7c7c80;
  --line:rgba(255,255,255,.13); --line-strong:rgba(255,255,255,.22);
  --tint:#0a84ff; --tint-soft:rgba(10,132,255,.18);
  --pending:#ffd426; --pending-bg:rgba(255,212,38,.14);
  --failed:#ff6961; --failed-bg:rgba(255,105,97,.13);
  --chrome:rgba(0,0,0,.72);
  --press:rgba(255,255,255,.08);
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.5);
}}}}
*{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%}}
body{{
  margin:0;background:var(--bg);color:var(--fg);
  font:400 17px/1.47 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif;
  letter-spacing:-.01em;                    /* body sits near zero */
  -webkit-font-smoothing:antialiased;
  padding:0 max(1rem,env(safe-area-inset-left)) calc(3rem + env(safe-area-inset-bottom));
}}
.shell{{max-width:44rem;margin:0 auto}}
/* Larger type takes progressively tighter tracking. */
h1{{font-size:1.75rem;line-height:1.14;letter-spacing:-.024em;font-weight:700;margin:.1rem 0 .9rem}}
h2{{font-size:.8125rem;line-height:1.3;letter-spacing:.05em;text-transform:uppercase;
  font-weight:600;color:var(--faint);margin:2rem 0 .5rem}}
h3{{font-size:1.0625rem;line-height:1.28;letter-spacing:-.016em;font-weight:600;margin:.15rem 0}}
p{{margin:0 0 .85rem}}
a{{color:inherit;text-decoration:none}}
ul{{margin:.2rem 0;padding-left:1.15rem}} li{{margin:.35rem 0}}

/* Chrome floats; content scrolls beneath it. */
.top{{
  position:sticky;top:0;z-index:10;display:flex;justify-content:center;
  margin:0 calc(-1 * max(1rem,env(safe-area-inset-left)));
  padding:calc(.55rem + env(safe-area-inset-top)) 1rem .55rem;
  background:var(--chrome);
  -webkit-backdrop-filter:saturate(180%) blur(20px);
  backdrop-filter:saturate(180%) blur(20px);
}}
.top::after{{content:"";position:absolute;left:0;right:0;bottom:-14px;height:14px;
  background:linear-gradient(var(--chrome),transparent);pointer-events:none}}

/* Controls sit where the thumb is. */
.tabs{{
  position:fixed;left:0;right:0;bottom:0;z-index:20;
  display:grid;grid-template-columns:repeat(5,1fr);
  padding:.35rem .4rem calc(.3rem + env(safe-area-inset-bottom));
  background:var(--chrome);
  -webkit-backdrop-filter:saturate(180%) blur(22px);
  backdrop-filter:saturate(180%) blur(22px);
  box-shadow:0 -.5px 0 var(--line);
}}
.tab{{
  display:flex;flex-direction:column;align-items:center;gap:.12rem;
  padding:.4rem 0 .3rem;color:var(--faint);
  font-size:.625rem;font-weight:600;letter-spacing:.01em;
  transition:transform .12s cubic-bezier(.2,.8,.3,1),color .15s ease-out;
}}
.tab:active{{transform:scale(.9)}}
.tab.on{{color:var(--fg)}}
.tab svg{{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.7;
  stroke-linecap:round;stroke-linejoin:round}}
.tab.on svg rect:first-child{{fill:currentColor;stroke:currentColor}}
/* The tab bar floats over the list, so the last row needs room to clear it. */
body{{padding-bottom:calc(4.9rem + env(safe-area-inset-bottom))}}

/* The field the tab opened, docked above the bar it came from. */
.drawer{{
  position:fixed;left:0;right:0;z-index:19;
  bottom:calc(4.25rem + env(safe-area-inset-bottom));
  display:flex;align-items:center;gap:.5rem;
  padding:.55rem .75rem;
  background:var(--chrome);
  -webkit-backdrop-filter:saturate(180%) blur(22px);
  backdrop-filter:saturate(180%) blur(22px);
  box-shadow:0 -.5px 0 var(--line);
}}
.drawer input{{flex:1;min-width:0}}
.find{{position:relative}}
.find input{{padding-left:2.2rem;background:var(--press);border-color:transparent;
  border-radius:.65rem}}
.find input::-webkit-search-cancel-button{{display:none}}
.mag{{
  position:absolute;left:1.45rem;top:50%;width:14px;height:14px;
  margin-top:-9px;border:2px solid var(--faint);border-radius:50%;pointer-events:none;
}}
.mag::after{{content:"";position:absolute;right:-5px;bottom:-4px;width:7px;height:2px;
  background:var(--faint);transform:rotate(45deg);border-radius:2px}}
.clear{{
  flex:none;display:grid;place-items:center;width:44px;height:44px;
  color:var(--faint);font-size:1.5rem;line-height:1;
}}
.clear:active{{opacity:.5}}

/* What is being filtered, said plainly — the box that did it may be closed. */
.filtered{{
  display:flex;align-items:center;justify-content:space-between;gap:1rem;
  padding:.55rem .8rem;margin:.7rem 0 0;border-radius:.7rem;
  background:var(--tint-soft);font-size:.875rem;font-weight:510;
}}
.filtered a{{color:var(--tint);font-weight:600}}

.auth{{max-width:22rem;margin:0 auto;padding:14dvh 0 4rem;text-align:center}}
.auth h1{{font-size:2.25rem;letter-spacing:-.03em;margin:0 0 .2rem}}
.auth .tag{{color:var(--faint);font-size:.9375rem;margin:0 0 2rem}}
.auth form,.auth-form{{display:flex;flex-direction:column;gap:.6rem;text-align:left}}
.auth-form{{max-width:22rem;margin-top:.6rem}}
.auth button{{margin-top:.35rem}}
.swap{{margin:1.5rem 0 0;color:var(--faint);font-size:.9375rem}}
.swap a{{color:var(--tint);font-weight:590}}
.warn,.good{{padding:.65rem .85rem;border-radius:.7rem;font-size:.9375rem;
  margin:0 0 1.1rem;text-align:left}}
.warn{{background:var(--failed-bg);color:var(--failed)}}
.good{{background:var(--tint-soft);color:var(--tint)}}
.keybox{{background:var(--surface);border:1px solid var(--line);border-radius:.7rem;
  padding:.85rem 1rem;font-size:.8125rem;overflow-x:auto;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}}

input{{
  width:100%;min-height:44px;padding:.6rem .85rem;font:inherit;font-size:1.0625rem;
  border:1px solid var(--line);border-radius:.7rem;
  background:var(--surface);color:var(--fg);
  transition:border-color .15s ease,box-shadow .15s ease;
}}
input::placeholder{{color:var(--faint)}}
input:focus{{outline:none;border-color:var(--tint);
  box-shadow:0 0 0 3.5px color-mix(in srgb,var(--tint) 22%,transparent)}}
:focus-visible{{outline:2px solid var(--tint);outline-offset:2px}}

/* Filled and tinted, the way a system control looks — an outlined box with
   coloured text reads as a placeholder for a button. */
button{{
  font:inherit;font-weight:600;letter-spacing:-.01em;cursor:pointer;
  border:none;border-radius:.7rem;
  background:var(--tint);color:#fff;
  min-height:44px;padding:0 1.15rem;
  transition:transform .12s cubic-bezier(.2,.8,.3,1),filter .15s ease-out;
}}
button:active{{transform:scale(.96);filter:brightness(.88)}}
button.quiet{{background:var(--tint-soft);color:var(--tint)}}
.add{{display:flex;gap:.5rem;align-items:center}}
.add button{{flex:none}}
.add input{{margin:0}}

.wordmark{{font-size:1.1875rem;font-weight:700;letter-spacing:-.028em}}

.chips{{display:flex;gap:.45rem;overflow-x:auto;padding:.85rem 0 .15rem;
  scrollbar-width:none;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain}}
.chips::-webkit-scrollbar{{display:none}}
.chip{{
  flex:none;display:inline-flex;align-items:center;gap:.34rem;
  min-height:32px;padding:0 .8rem;border:1px solid var(--line);border-radius:999px;
  font-size:.875rem;font-weight:510;letter-spacing:-.006em;white-space:nowrap;
  background:var(--surface);
  transition:transform .1s ease-out,background-color .15s ease-out;
}}
.chip:active{{transform:scale(.96)}}
.chip span{{color:var(--faint);font-variant-numeric:tabular-nums}}
.chip.on{{background:var(--fg);color:var(--bg);border-color:var(--fg)}}
.chip.on span{{color:var(--bg);opacity:.65}}

.browse{{
  display:flex;align-items:center;justify-content:space-between;
  min-height:48px;padding:0 .95rem;margin:.85rem 0 .35rem;
  border-radius:.8rem;background:var(--surface);border:1px solid var(--line);
  font-size:.9375rem;font-weight:590;letter-spacing:-.01em;color:var(--tint);
  transition:transform .1s ease-out,background-color .15s ease-out;
}}
.browse:active{{transform:scale(.985);background:var(--press)}}
.browse .chev{{color:var(--faint);font-size:1.25rem}}
/* A grid of stills, the way the reels were saved in the first place. */
.grid{{display:grid;gap:.5rem;margin-top:.85rem;
  grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}}
@media(min-width:560px){{.grid{{gap:.65rem;
  grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}}}}
.tile{{
  position:relative;display:block;aspect-ratio:9/14;overflow:hidden;
  border-radius:.85rem;background:var(--surface);border:1px solid var(--line);
  transition:transform .1s ease-out;
}}
.tile:active{{transform:scale(.975)}}
.tile img{{width:100%;height:100%;object-fit:cover;display:block}}
.noshot{{position:absolute;inset:0;
  background:linear-gradient(150deg,var(--press),transparent)}}
.tile-text{{
  position:absolute;left:0;right:0;bottom:0;
  display:flex;flex-direction:column;gap:.15rem;padding:2.4rem .6rem .6rem;
  background:linear-gradient(transparent,rgba(0,0,0,.8));
}}
.tile-cat{{color:rgba(255,255,255,.7);font-size:.625rem;font-weight:660;
  letter-spacing:.08em;text-transform:uppercase}}
.tile-title{{color:#fff;font-size:.875rem;line-height:1.25;letter-spacing:-.012em;
  font-weight:620;text-shadow:0 1px 4px rgba(0,0,0,.5);
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
/* A note with no still yet: state first, then whatever we know. */
.tile.bare{{background:var(--surface)}}
.bare-in{{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:flex-start;justify-content:flex-end;gap:.4rem;padding:.7rem}}
.bare-msg{{color:var(--dim);font-size:.8125rem;line-height:1.35;
  display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}}
.meta{{color:var(--faint);font-size:.75rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.055em;margin:0 0 .12rem}}
.lede{{margin:.18rem 0 0;color:var(--dim);font-size:.9375rem;line-height:1.42}}
.empty{{color:var(--faint);padding:2.5rem 0;text-align:center}}

/* State reads as shape and colour, not only as words. */
.pill{{display:inline-flex;align-items:center;gap:.35rem;padding:.15rem .5rem;
  border-radius:999px;font-size:.75rem;font-weight:600;letter-spacing:.01em;
  text-transform:none}}
.pill.pending{{color:var(--pending);background:var(--pending-bg)}}
.pill.failed{{color:var(--failed);background:var(--failed-bg)}}
.pill .dot{{width:6px;height:6px;border-radius:50%;background:currentColor}}
.pill.pending .dot{{animation:pulse 1.8s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}

.back{{display:inline-flex;align-items:center;gap:.2rem;min-height:44px;
  color:var(--tint);font-size:1.0625rem;letter-spacing:-.01em}}
.back:active{{opacity:.55}}
.note-meta{{color:var(--dim);font-size:.9375rem;margin:.15rem 0 1rem}}
.note-meta a{{color:var(--tint)}}
.lede-lg{{font-size:1.1875rem;line-height:1.4;letter-spacing:-.014em;color:var(--dim);
  margin:0 0 .3rem}}
/* Grouped rows, the way a settings list looks: one surface, hairline between. */
.row,.row-d>summary{{
  display:flex;align-items:center;justify-content:space-between;gap:.75rem;
  min-height:48px;padding:0 1rem;background:var(--surface);
  border:1px solid var(--line);border-radius:.8rem;
  font-size:1rem;font-weight:510;letter-spacing:-.01em;color:var(--fg);
  cursor:pointer;list-style:none;
  transition:transform .12s cubic-bezier(.2,.8,.3,1),background-color .15s ease-out;
}}
.row{{margin-top:.55rem}}
.row-d{{margin-top:.55rem}}
.row:active,.row-d>summary:active{{transform:scale(.985);background:var(--press)}}
.row-d>summary::-webkit-details-marker{{display:none}}
.chev{{color:var(--faint);font-size:1.2rem;line-height:1}}
.row-d[open]>summary{{border-radius:.8rem .8rem 0 0;border-bottom-color:transparent}}
.row-d[open]>summary .chev{{transform:rotate(90deg)}}
.row-d>p{{margin:0;padding:.9rem 1rem 1rem;background:var(--surface);
  border:1px solid var(--line);border-top:none;border-radius:0 0 .8rem .8rem;
  color:var(--dim);font-size:.9375rem;line-height:1.5}}

.actions{{display:flex;gap:.55rem;margin:2.25rem 0 1rem}}
.actions button{{background:var(--tint-soft);color:var(--tint)}}
.actions button.danger{{background:var(--failed-bg);color:var(--failed)}}

/* ---- Feed: one note per screen, in the shape the reel arrived in ---- */
body.bleed{{padding:0;overflow:hidden}}
body.bleed .shell{{max-width:none}}
/* Sideways for cards, down for reading. Two axes, two jobs, and neither
   gesture has to guess which one you meant — which is what made the vertical
   version fight your finger. */
.feed{{
  height:100dvh;display:flex;
  overflow-x:auto;overflow-y:hidden;
  scroll-snap-type:x mandatory;overscroll-behavior-x:contain;
  -webkit-overflow-scrolling:touch;scrollbar-width:none;
}}
.feed::-webkit-scrollbar{{display:none}}
.feed-back{{position:fixed;top:max(.5rem,env(safe-area-inset-top));left:.85rem;z-index:20;
  padding:0 .8rem;border-radius:999px;color:#fff;
  background:rgba(0,0,0,.42);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px)}}
/* One card per screen, but the card grows with its note and the page does all
   the scrolling. Nesting a scroller inside a snap container was the jank. */
.reel{{
  width:100vw;flex:none;height:100dvh;
  scroll-snap-align:start;scroll-snap-stop:always;overflow:hidden;
  display:flex;flex-direction:column;gap:.9rem;
  padding:calc(3.25rem + env(safe-area-inset-top)) .9rem
          calc(2rem + env(safe-area-inset-bottom));
}}
/* A poster, sized like a poster. It was eating half the screen to show a
   still nobody needs at that scale. */
.stage{{
  position:relative;display:block;flex:none;
  height:34dvh;min-height:190px;max-height:300px;
  border-radius:1rem;overflow:hidden;background:#000;
  border:1px solid var(--line);
}}
.stage .blur{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
  filter:blur(30px) saturate(150%) brightness(.45);transform:scale(1.25)}}
.stage .poster{{position:relative;margin:0 auto;height:100%;width:auto;max-width:100%;
  aspect-ratio:9/16;object-fit:cover;display:block}}
.play{{
  position:absolute;top:50%;left:50%;width:54px;height:54px;margin:-27px 0 0 -27px;
  border-radius:50%;background:rgba(255,255,255,.18);
  -webkit-backdrop-filter:blur(18px) saturate(180%);backdrop-filter:blur(18px) saturate(180%);
  border:.5px solid rgba(255,255,255,.45);z-index:2;
  box-shadow:0 2px 14px rgba(0,0,0,.3);
  transition:transform .12s ease-out,background-color .15s ease-out;
}}
.play::before{{content:"";position:absolute;top:50%;left:55%;transform:translate(-50%,-50%);
  border-style:solid;border-width:8px 0 8px 13px;
  border-color:transparent transparent transparent #fff}}
.stage:active .play{{transform:scale(.9);background:rgba(255,255,255,.3)}}
/* A capsule that reads as a control, not a caption stranded on the artwork. */
.watch{{
  position:absolute;right:.55rem;bottom:.55rem;z-index:2;
  padding:.3rem .7rem;border-radius:999px;
  font-size:.75rem;font-weight:600;letter-spacing:-.003em;color:#fff;
  background:rgba(0,0,0,.5);
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
  border:.5px solid rgba(255,255,255,.22);
}}
.sheet{{
  flex:1;min-height:0;
  overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;
  scrollbar-width:none;
  padding:1.05rem 1.15rem 1.25rem;
  background:var(--surface);border:1px solid var(--line);border-radius:1rem;
}}
.sheet::-webkit-scrollbar{{display:none}}

/* Read more, as a sheet over the card rather than more page to scroll. */
.modal{{display:none}}
.modal:target{{
  display:block;position:fixed;inset:0;z-index:40;
}}
.scrim{{position:absolute;inset:0;background:rgba(0,0,0,.45);
  -webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px)}}
.modal-card{{
  position:absolute;left:0;right:0;bottom:0;max-height:88dvh;
  display:flex;flex-direction:column;
  background:var(--bg);border-radius:1.15rem 1.15rem 0 0;
  box-shadow:0 -8px 40px rgba(0,0,0,.35);
  animation:rise .34s cubic-bezier(.2,.9,.3,1);
}}
@keyframes rise{{from{{transform:translateY(14%);opacity:.6}}to{{transform:none;opacity:1}}}}
.grabber{{
  display:block;flex:none;height:26px;position:relative;
}}
.grabber::before{{content:"";position:absolute;top:9px;left:50%;margin-left:-18px;
  width:36px;height:5px;border-radius:3px;background:var(--rule-2,var(--line-strong))}}
.modal-body{{
  overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;
  padding:.35rem 1.15rem calc(1.5rem + env(safe-area-inset-bottom));
}}
.sub{{font-size:.75rem;font-weight:660;letter-spacing:.055em;text-transform:uppercase;
  color:var(--faint);margin:1.4rem 0 .3rem}}
@media(min-width:820px){{
  .modal-card{{left:50%;right:auto;bottom:auto;top:50%;
    transform:translate(-50%,-50%);width:min(38rem,92vw);
    border-radius:1.15rem;max-height:82dvh}}
  @keyframes rise{{from{{transform:translate(-50%,-46%);opacity:.6}}
    to{{transform:translate(-50%,-50%);opacity:1}}}}
}}
/* Desktop: stop stacking. The reel keeps its portrait shape beside the note
   instead of stretching a phone layout across a monitor. */
@media(min-width:820px){{
  .reel{{
    flex-direction:row;align-items:center;gap:1.5rem;
    padding:1.5rem clamp(2rem,8vw,7rem);
  }}
  .stage{{height:min(74dvh,620px);max-height:none;flex:none;
    aspect-ratio:9/16;border-radius:1.1rem;
    box-shadow:0 20px 60px rgba(0,0,0,.35)}}
  .sheet{{flex:1;align-self:stretch;display:flex;flex-direction:column;
    justify-content:center;padding:2rem 2.15rem}}
  .feed-title{{font-size:1.5rem;letter-spacing:-.022em}}
}}
.sheet::-webkit-scrollbar{{display:none}}
.feed-title{{font-size:1.3125rem;line-height:1.2;letter-spacing:-.021em;font-weight:700;
  color:var(--fg);text-transform:none;margin:.1rem 0 .35rem}}
.sheet .lede{{font-size:1rem;line-height:1.42;margin:0 0 .75rem}}
.takeaways,.steps{{margin:.25rem 0 .8rem;padding-left:1.1rem;font-size:.9375rem;line-height:1.45}}
.takeaways li,.steps li{{margin:.3rem 0}}
.facts{{display:flex;flex-wrap:wrap;gap:.4rem;margin:.15rem 0 .85rem}}
.fact{{padding:.35rem .6rem;border-radius:.6rem;background:var(--surface);
  border:1px solid var(--line)}}
.fact dt{{font-size:.6875rem;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
  color:var(--faint);margin:0}}
.fact dd{{margin:.1rem 0 0;font-size:.9375rem;font-weight:600;letter-spacing:-.01em;
  font-variant-numeric:tabular-nums}}
.full{{display:inline-flex;min-height:44px;align-items:center;color:var(--tint);
  font-size:.9375rem;font-weight:510}}

@media(prefers-reduced-motion:reduce){{
  *,*::before,*::after{{animation-duration:.01ms !important;animation-iteration-count:1 !important;
    transition-duration:.01ms !important}}
  button:active,.tile:active,.chip:active,.browse:active,
  .stage:active .play{{transform:none}}
  .feed{{scroll-behavior:auto}}
}}
@media(prefers-reduced-transparency:reduce){{
  .bar{{background:var(--bg);-webkit-backdrop-filter:none;backdrop-filter:none}}
  .bar::after{{display:none}}
}}
@media(prefers-contrast:more){{
  :root{{--line:rgba(0,0,0,.4);--line-strong:rgba(0,0,0,.6);--dim:#3a3a3c;--faint:#4a4a4e}}
  @media(prefers-color-scheme:dark){{
    :root{{--line:rgba(255,255,255,.45);--line-strong:rgba(255,255,255,.7);
      --dim:#d8d8dc;--faint:#c0c0c6}}
  }}
}}
</style></head><body class="{cls}"><div class=shell>{body}</div></body></html>"""
