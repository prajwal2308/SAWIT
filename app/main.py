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
from urllib.parse import urlencode

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

from . import instagram
from .config import Settings, get_settings
from .pipeline import Source, process
from .store import Store

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Fail fast on missing config rather than at the first shared reel, and
    # clear out anything a restart left mid-flight.
    settings = get_settings()
    orphans = get_store().recover_orphans()
    if orphans:
        log.warning("Marked %d interrupted note(s) as failed; they can be retried", orphans)
    log.info(
        "Sawit ready — instagram=%s push=%s asr=%s",
        settings.instagram_enabled, settings.push_enabled, settings.asr_backend,
    )
    yield


app = FastAPI(title="Sawit", docs_url=None, redoc_url=None, lifespan=lifespan)


@lru_cache(maxsize=1)
def get_store() -> Store:
    return Store(get_settings().db_path)


# A key in ?k= is a key in your history, your bookmarks and every referer the
# page sends. One visit with ?k= trades it for this cookie, and the links the
# pages generate go clean from the next request on.
KEY_COOKIE = "sawit_key"
KEY_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def require_key(
    request: Request,
    k: str | None = Query(default=None, description="Key for browser views."),
    settings: Settings = Depends(get_settings),
) -> None:
    """Accept the key as a header (Shortcut), ?k= (first visit) or the cookie."""
    supplied = request.headers.get("x-api-key") or k or request.cookies.get(KEY_COOKIE) or ""
    # compare_digest rejects non-ASCII outright rather than comparing it.
    if not supplied.isascii() or not secrets.compare_digest(supplied, settings.api_key):
        raise HTTPException(status_code=401, detail="Bad or missing API key.")
    if k and request.cookies.get(KEY_COOKIE) != supplied:
        # Set on the way out, where the finished response is.
        request.state.grant_cookie = supplied


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
    note_id = store.create_pending(url)
    background.add_task(process, note_id, Source(page_url=url), settings, store)
    return _redirect_or_json(request, k, f"/notes/{note_id}",
                             {"id": note_id, "status": "pending"})


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
    store: Store = Depends(get_store),
) -> list[dict[str, Any]]:
    if q:
        return store.search(q, limit, category=category)
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
    store: Store = Depends(get_store),
) -> HTMLResponse:
    if q:
        notes = store.search(q, 100, category=category)
    else:
        notes = store.recent(100, category=category)

    link_key = _link_key(request)
    key = html.escape(link_key, quote=True)
    chips = _category_chips(store.category_counts(), category, link_key, q)
    if notes:
        rows = "\n".join(_card(note, key) for note in notes)
    elif category:
        rows = f"<p class=empty>Nothing in {html.escape(category)} yet.</p>"
    else:
        rows = "<p class=empty>Nothing saved yet.</p>"

    # Omitted entirely once the cookie carries it: a hidden field with an empty
    # value still puts a bare `k=` on every search you run.
    key_field = f'<input type=hidden name=k value="{key}">' if key else ""
    return HTMLResponse(_page(
        f"""<div class=bar>
              <form method=post action="/add{html.escape(_qs(k=link_key), quote=True)}"
                    class=add>
                <input name=url type=url required
                       placeholder="Paste a reel link" autocomplete=off
                       autocapitalize=off autocorrect=off spellcheck=false>
                <button>Save</button>
              </form>
              <form method=get>
                {key_field}
                <input type=hidden name=category value="{html.escape(category or '', quote=True)}">
                <input name=q value="{html.escape(q or '', quote=True)}" type=search
                       placeholder="Search everything you saved" autocomplete=off
                       style="margin-top:.5rem">
              </form>
              {chips}
            </div>
            {rows}"""
    ))


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
        f"<p class=note-meta style='margin-top:1.75rem'>"
        f"<a href='{esc(note['url'])}'>Open the original reel &rsaquo;</a></p>",
        f"<details><summary>Transcript</summary><p>{esc(note['transcript'])}</p></details>"
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
        return (f"<a class=card href='/notes/{note_id}{qs}'>"
                f"<div>{pill}"
                f"<p class=lede>{esc(note.get('error') or note['url'])}</p></div></a>")
    thumb = (f"<img src='/notes/{note_id}/thumb.jpg{qs}' alt='' loading=lazy>"
             if note["has_thumbnail"] else "")
    return (f"<a class=card href='/notes/{note_id}{qs}'>{thumb}"
            f"<div><p class=meta>{esc(note['category'])}</p>"
            f"<h3>{esc(note['title'])}</h3>"
            f"<p class=lede>{esc(note['one_liner'])}</p></div></a>")


def _page(body: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#fbfbfd" media="(prefers-color-scheme:light)">
<meta name=theme-color content="#000000" media="(prefers-color-scheme:dark)">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-title content=Sawit>
<title>Sawit</title><style>
:root{{
  color-scheme:light dark;
  --bg:#fbfbfd; --surface:#fff; --fg:#1d1d1f; --dim:#6e6e73; --faint:#8e8e93;
  --line:rgba(0,0,0,.10); --line-strong:rgba(0,0,0,.16);
  --tint:#0071e3;            /* interactive */
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
  --tint:#0a84ff;
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
.bar{{
  position:sticky;top:0;z-index:10;
  margin:0 calc(-1 * max(1rem,env(safe-area-inset-left)));
  padding:max(.6rem,env(safe-area-inset-top)) max(1rem,env(safe-area-inset-left)) .55rem;
  background:var(--chrome);
  -webkit-backdrop-filter:saturate(180%) blur(20px);
  backdrop-filter:saturate(180%) blur(20px);
}}
/* A fade where content meets floating chrome, not a hard rule. */
.bar::after{{content:"";position:absolute;left:0;right:0;bottom:-12px;height:12px;
  background:linear-gradient(var(--chrome),transparent);pointer-events:none}}

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

button{{
  font:inherit;font-weight:590;letter-spacing:-.01em;cursor:pointer;
  border:1px solid var(--line-strong);border-radius:.7rem;
  background:var(--surface);color:var(--fg);
  min-height:44px;padding:0 1.05rem;
  transition:transform .1s ease-out,background-color .1s ease-out;
}}
/* Feedback belongs on the press, and it is immediate. */
button:active{{transform:scale(.97);background:var(--press)}}
.add{{display:flex;gap:.5rem;align-items:center}}
.add button{{flex:none;color:var(--tint);border-color:var(--tint)}}
.add input{{margin:0}}

.chips{{display:flex;gap:.45rem;overflow-x:auto;padding:.55rem 0 .15rem;
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

.card{{
  display:flex;gap:.85rem;align-items:flex-start;
  padding:.85rem;margin:0 -.85rem;border-radius:.9rem;
  transition:transform .1s ease-out,background-color .15s ease-out;
}}
.card + .card{{box-shadow:0 -1px 0 var(--line)}}
.card:active{{transform:scale(.985);background:var(--press)}}
.card img{{width:66px;height:88px;object-fit:cover;border-radius:.6rem;flex:none;
  background:var(--press)}}
.card > div{{min-width:0;flex:1}}
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
details{{margin-top:1.75rem;color:var(--dim);font-size:.9375rem}}
summary{{cursor:pointer;min-height:44px;display:flex;align-items:center;
  color:var(--tint);font-weight:510}}

.actions{{display:flex;gap:.55rem;margin:2.25rem 0 1rem}}
.actions button.danger{{color:var(--failed);
  border-color:color-mix(in srgb,var(--failed) 45%,transparent)}}

@media(prefers-reduced-motion:reduce){{
  *,*::before,*::after{{animation-duration:.01ms !important;animation-iteration-count:1 !important;
    transition-duration:.01ms !important}}
  button:active,.card:active,.chip:active{{transform:none}}
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
</style></head><body><div class=shell>{body}</div></body></html>"""
