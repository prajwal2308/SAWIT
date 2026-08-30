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

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, field_validator

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


def require_key(
    request: Request,
    k: str | None = Query(default=None, description="Key for browser views."),
    settings: Settings = Depends(get_settings),
) -> None:
    """Accept the key as a header (Shortcut) or ?k= (phone browser)."""
    supplied = request.headers.get("x-api-key") or k or ""
    if not secrets.compare_digest(supplied, settings.api_key):
        raise HTTPException(status_code=401, detail="Bad or missing API key.")


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
        "model": settings.model,
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
    return _redirect_or_json(k, f"/notes/{note_id}", {"id": note_id, "status": "pending"})


@app.post("/notes/{note_id}/delete", dependencies=[Depends(require_key)])
def delete_note_form(
    note_id: str, k: str | None = None, store: Store = Depends(get_store)
) -> Response:
    if not store.delete(note_id):
        raise HTTPException(status_code=404, detail="No such note.")
    return _redirect_or_json(k, "/", {"deleted": note_id})


@app.delete("/api/notes/{note_id}", dependencies=[Depends(require_key)])
def delete_note(note_id: str, store: Store = Depends(get_store)) -> dict[str, str]:
    if not store.delete(note_id):
        raise HTTPException(status_code=404, detail="No such note.")
    return {"deleted": note_id}


def _redirect_or_json(k: str | None, path: str, payload: dict) -> Response:
    """Browsers came from a form and want a page; the API wants JSON."""
    if k:
        return RedirectResponse(f"{path}?k={k}", status_code=303)
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
    q: str | None = None,
    category: str | None = None,
    k: str | None = None,
    store: Store = Depends(get_store),
) -> HTMLResponse:
    if q:
        notes = store.search(q, 100, category=category)
    else:
        notes = store.recent(100, category=category)

    key = html.escape(k or "", quote=True)
    chips = _category_chips(store.category_counts(), category, k, q)
    if notes:
        rows = "\n".join(_card(note, key) for note in notes)
    elif category:
        rows = f"<p class=empty>Nothing in {html.escape(category)} yet.</p>"
    else:
        rows = "<p class=empty>Nothing saved yet.</p>"

    return HTMLResponse(_page(
        f"""<form method=get>
              <input type=hidden name=k value="{key}">
              <input type=hidden name=category value="{html.escape(category or '', quote=True)}">
              <input name=q value="{html.escape(q or '', quote=True)}"
                     placeholder="Search everything you saved" autocomplete=off>
            </form>
            {chips}
            {rows}"""
    ))


def _category_chips(
    counts: list[tuple[str, int]], active: str | None, key: str | None, q: str | None
) -> str:
    """One tap to narrow to a topic, without losing the current search."""
    if not counts:
        return ""

    def chip(label: str, value: str | None, count: int) -> str:
        params = {p: v for p, v in (("k", key), ("q", q), ("category", value)) if v}
        # escape() the query string too: a bare & in an href is invalid HTML.
        href = html.escape(f"/?{urlencode(params)}", quote=True)
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
    k: str | None = None,
    store: Store = Depends(get_store),
) -> HTMLResponse:
    note = store.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="No such note.")
    key = html.escape(k or "", quote=True)
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731

    if note["status"] != "ready":
        detail = esc(note.get("error") or "Still working on it.")
        return HTMLResponse(_page(
            f"<a href='/?k={key}'>&larr; all notes</a>"
            f"<h1>{esc(note['status'])}</h1><p>{detail}</p>"
            f"<p><a href='{esc(note['url'])}'>The original reel</a></p>"
            f"{_actions(note, key)}"
        ))

    def section(heading: str, items: list, render) -> str:
        if not items:
            return ""
        body = "".join(render(i) for i in items)
        return f"<h2>{heading}</h2><ul>{body}</ul>"

    parts = [
        f"<a href='/?k={key}'>&larr; all notes</a>",
        f"<h1>{esc(note['title'])}</h1>",
        f"<p class=meta><a href='/?k={key}&category={esc(note['category'])}'>"
        f"{esc(note['category'])}</a>"
        + (f" &middot; {esc(note['uploader'])}" if note.get("uploader") else "")
        + "</p>",
        f"<p class=lede>{esc(note['one_liner'])}</p>",
        section("Takeaways", note["takeaways"], lambda t: f"<li>{esc(t)}</li>"),
        section("Steps", note["steps"], lambda s: f"<li>{esc(s)}</li>"),
        section("Key facts", note["key_facts"],
                lambda f: f"<li><b>{esc(f['label'])}:</b> {esc(f['value'])}</li>"),
        section("Caveats", note["caveats"], lambda c: f"<li>{esc(c)}</li>"),
        f"<p><a href='{esc(note['url'])}'>Open the original reel</a></p>",
        f"<details><summary>Transcript</summary><p>{esc(note['transcript'])}</p></details>",
        _actions(note, key),
    ]
    return HTMLResponse(_page("".join(parts)))


def _actions(note: dict[str, Any], key: str) -> str:
    """Retry and delete. Forms, not links — a link that deletes is a trap for
    anything that prefetches."""
    note_id = html.escape(note["id"])
    buttons = ""
    if note["status"] == "failed":
        buttons += (f"<form method=post action='/notes/{note_id}/retry?k={key}'>"
                    f"<button>Retry</button></form>")
    buttons += (f"<form method=post action='/notes/{note_id}/delete?k={key}' "
                f"onsubmit=\"return confirm('Delete this note?')\">"
                f"<button class=danger>Delete</button></form>")
    return f"<div class=actions>{buttons}</div>"


def _card(note: dict[str, Any], key: str) -> str:
    esc = html.escape
    note_id = esc(note["id"])
    if note["status"] != "ready":
        label = "failed" if note["status"] == "failed" else "working…"
        return (f"<a class=card href='/notes/{note_id}?k={key}'>"
                f"<div><p class=meta>{label}</p>"
                f"<p class=lede>{esc(note.get('error') or note['url'])}</p></div></a>")
    thumb = (f"<img src='/notes/{note_id}/thumb.jpg?k={key}' alt='' loading=lazy>"
             if note["has_thumbnail"] else "")
    return (f"<a class=card href='/notes/{note_id}?k={key}'>{thumb}"
            f"<div><p class=meta>{esc(note['category'])}</p>"
            f"<h3>{esc(note['title'])}</h3>"
            f"<p class=lede>{esc(note['one_liner'])}</p></div></a>")


def _page(body: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Sawit</title><style>
:root{{color-scheme:light dark;--fg:#111;--dim:#666;--line:#e5e5e5;--bg:#fff}}
@media(prefers-color-scheme:dark){{:root{{--fg:#eee;--dim:#999;--line:#2a2a2a;--bg:#111}}}}
*{{box-sizing:border-box}}
body{{margin:0 auto;padding:1rem;max-width:44rem;background:var(--bg);color:var(--fg);
font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
h1{{font-size:1.5rem;margin:.4rem 0}} h2{{font-size:1rem;margin:1.4rem 0 .4rem}}
h3{{font-size:1.05rem;margin:.2rem 0}}
input{{width:100%;padding:.7rem;font-size:1rem;border:1px solid var(--line);
border-radius:.6rem;background:transparent;color:var(--fg);margin-bottom:1rem}}
a{{color:inherit}} ul{{margin:.3rem 0;padding-left:1.2rem}} li{{margin:.3rem 0}}
.card{{display:flex;gap:.8rem;padding:.8rem 0;border-bottom:1px solid var(--line);
text-decoration:none}}
.card img{{width:72px;height:96px;object-fit:cover;border-radius:.5rem;flex:none}}
.meta{{color:var(--dim);font-size:.8rem;text-transform:uppercase;
letter-spacing:.04em;margin:0}}
.lede{{margin:.25rem 0;color:var(--dim)}} .empty{{color:var(--dim)}}
.chips{{display:flex;gap:.4rem;overflow-x:auto;padding-bottom:.6rem;
margin-bottom:.4rem;-webkit-overflow-scrolling:touch}}
.chip{{flex:none;padding:.35rem .7rem;border:1px solid var(--line);border-radius:999px;
font-size:.85rem;text-decoration:none;white-space:nowrap}}
.chip span{{color:var(--dim)}}
.chip.on{{background:var(--fg);color:var(--bg);border-color:var(--fg)}}
.chip.on span{{color:var(--bg);opacity:.7}}
details{{margin-top:1.5rem;color:var(--dim)}}
.actions{{display:flex;gap:.5rem;margin:2rem 0 1rem}}
.actions button{{padding:.5rem .9rem;font-size:.9rem;border:1px solid var(--line);
border-radius:.5rem;background:transparent;color:var(--fg);cursor:pointer}}
.actions button.danger{{color:#c0392b;border-color:#c0392b}}
</style></head><body>{body}</body></html>"""
