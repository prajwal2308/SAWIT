"""HTTP surface: one write endpoint for the Shortcut, a few read views for you."""

from __future__ import annotations

import html
import secrets
from functools import lru_cache
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, field_validator

from . import instagram
from .config import Settings, get_settings
from .pipeline import Source, process
from .store import Store

app = FastAPI(title="Sawit", docs_url=None, redoc_url=None)


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
    return {"status": "ok"}


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
    limit: int = 50,
    store: Store = Depends(get_store),
) -> list[dict[str, Any]]:
    return store.search(q, limit) if q else store.recent(limit)


@app.get("/api/notes/{note_id}", dependencies=[Depends(require_key)])
def api_note(note_id: str, store: Store = Depends(get_store)) -> dict[str, Any]:
    note = store.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="No such note.")
    return note


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
    k: str | None = None,
    store: Store = Depends(get_store),
) -> HTMLResponse:
    notes = store.search(q, 100) if q else store.recent(100)
    key = html.escape(k or "", quote=True)
    rows = "\n".join(_card(note, key) for note in notes) or "<p class=empty>Nothing saved yet.</p>"
    return HTMLResponse(_page(
        f"""<form method=get>
              <input type=hidden name=k value="{key}">
              <input name=q value="{html.escape(q or '', quote=True)}"
                     placeholder="Search everything you saved" autocomplete=off>
            </form>
            {rows}"""
    ))


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
        return HTMLResponse(_page(f"<a href='/?k={key}'>&larr; all notes</a>"
                                  f"<h1>{esc(note['status'])}</h1><p>{detail}</p>"))

    def section(heading: str, items: list, render) -> str:
        if not items:
            return ""
        body = "".join(render(i) for i in items)
        return f"<h2>{heading}</h2><ul>{body}</ul>"

    parts = [
        f"<a href='/?k={key}'>&larr; all notes</a>",
        f"<h1>{esc(note['title'])}</h1>",
        f"<p class=meta>{esc(note['category'])}"
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
    ]
    return HTMLResponse(_page("".join(parts)))


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
details{{margin-top:1.5rem;color:var(--dim)}}
</style></head><body>{body}</body></html>"""
