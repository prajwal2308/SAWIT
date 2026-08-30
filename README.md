# Sawit

*Saw it for you.*

Send a reel to it. Get the point back. Never rewatch.

Deliberately **not** an App Store app. There are two ways in, and neither needs
an Apple Developer account, a Mac, or App Store review.

**1. Send it as a DM (best).** The account is an Instagram professional account.
You share a reel to it exactly like sending it to a friend — same gesture, same
place in the share sheet, never leaving Instagram — and the takeaway comes back
as a reply in that same thread. Meta's webhook hands over the media directly, so
there is no scraping and nothing to break.

**2. The iOS share sheet (fallback).** A Shortcut posts the link to the server
and closes instantly; the answer arrives as a push notification. This covers
Facebook reels, and anything else with a URL.

## How it works

```
 DM to the account ──► webhook ──┐        (Meta hands over the media URL)
                                 ├─► 200/202 immediately, nothing to wait on
 share sheet ─► Shortcut ─► /ingest ──┘   (yt-dlp scrapes the page URL)
                                 │
               ffmpeg ───────────┤ 16 kHz mono audio + 4 stills
               whisper ──────────┤ transcript
               Claude ───────────┤ typed note (title, takeaways,
                                 │   steps, key facts, caveats)
               SQLite + FTS5 ────┤ stored and searchable
                                 └─► reply in the DM thread, or push via ntfy
```

Two design decisions worth knowing:

**The stills are not decoration.** Reels routinely put the numbers, formulas and
lists on screen and never say them out loud. Four frames go to Claude alongside
the transcript, so a "how to calculate X" reel yields the actual calculation.

**One note schema, not one per category.** Category-specific guidance lives in
the prompt; the schema stays flat. The `steps` field is the one that earns its
keep — it is the difference between "explains a budgeting rule" and a procedure
you can follow without opening the video again.

## The honest caveat

The two paths have completely different risk profiles, which is why both exist.

**The DM path is sanctioned.** Meta's webhook delivers an `ig_reel` attachment
with a direct media URL. No scraping, no cookies, nothing that breaks when
Instagram reshuffles its HTML. The real constraints are Meta's rules, not
Meta's defences:

- An Instagram **professional** account (Business or Creator) is required.
  Personal accounts have no messaging API.
- **You can use it today for yourself.** Up to 25 test users work without App
  Review — add your own account as a tester and it just works.
- Going beyond that needs `instagram_business_manage_messages` at Advanced
  Access, which means App Review (roughly 5–10 business days). This is the real
  gate on other people using it, and it is worth knowing before you plan for
  users.
- The **24-hour window**: you may reply freely within 24 hours of someone's
  message. Answering a reel they just sent is comfortably inside it. You cannot
  message them unprompted days later.
- The CDN URLs expire quickly, so the download happens immediately. A failed
  fetch is not worth retrying later.

**The share-sheet path is scraping.** yt-dlp against URLs you already have
access to: needs a logged-in cookie jar, will break periodically, and is against
Meta's ToS. Fine for archiving your own saves to your own server; think hard
before pointing it at other people. Prefer the DM path wherever it works.

Nothing here rehosts video on either path. Only derived notes, one transcript,
and one thumbnail per reel are stored.

## Run it locally

```bash
cd sawit
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # needs ffmpeg on PATH
cp .env.example .env                     # then fill it in
set -a && source .env && set +a
uvicorn app.main:app --reload
```

Open `http://localhost:8000/?k=$SAWIT_API_KEY`.

`SAWIT_API_KEY` is required — the service refuses to start without one. It
downloads and transcribes whatever URL it is handed, so an unauthenticated
instance is somebody else's free compute.

## Setting up the DM path

1. Convert the account you want to share to into an Instagram **professional**
   account (Settings → Account type).
2. Create an app at developers.facebook.com, add the **Instagram** product, and
   connect that account.
3. Add your own Instagram account as a **tester** on the app. This is what lets
   you use it immediately without App Review.
4. Subscribe a webhook to the `messages` field, pointing at
   `https://your-app/webhook/instagram`. Paste your `IG_VERIFY_TOKEN` into the
   console when it asks for a verify token — the `GET` handshake answers it.
5. Copy the app secret into `IG_APP_SECRET` and a long-lived access token into
   `IG_ACCESS_TOKEN`.

Then share a reel to the account from your personal one. Every webhook is
checked against `X-Hub-Signature-256` before anything is queued, and each
message is deduplicated by its `mid` — Meta retries deliveries, and without that
you would download, transcribe and pay for the same reel twice.

If the send call is rejected, check `IG_API_VERSION` first; the endpoint shape
moves between Graph versions and is config here, not code.

## Cookies

Most Instagram and Facebook URLs return a login wall to a server. Export cookies
from a browser where you are logged in (any "Netscape format" cookie exporter
extension), save as `cookies.txt`, and point `YTDLP_COOKIES_FILE` at it. Treat
that file like a password — it *is* your session.

## Deploy

The `Dockerfile` installs ffmpeg and runs uvicorn on `$PORT`; it works as-is on
Railway, Fly, or Render.

```bash
fly launch --dockerfile Dockerfile
fly volumes create data --size 1          # notes must outlive a redeploy
fly secrets set SAWIT_API_KEY=... ANTHROPIC_API_KEY=... NTFY_TOPIC=...
```

Mount the volume at `/data` and set `SAWIT_DB=/data/sawit.sqlite3`. Set
`PUBLIC_BASE_URL` to your deployed URL so notifications are tappable.

## Push notifications

Install the **ntfy** app (free, iOS and Android) and subscribe to the topic you
set as `NTFY_TOPIC`. Anyone who guesses the topic name can read your notes, so
make it long and random.

This is a stand-in for APNs. It costs nothing and needs no developer account;
the tradeoff is a generic app icon on the notification.

## The iOS Shortcut

1. **Shortcuts** app → **+** → name it *Save Reel*.
2. Open shortcut details (ⓘ) → turn on **Show in Share Sheet**. Set accepted
   input to **URLs** and **Text** (Instagram sometimes shares text with the URL
   inside it; the server pulls the URL out either way).
3. Add one action: **Get Contents of URL**.
   - URL: `https://your-app.fly.dev/ingest`
   - Method: **POST**
   - Headers: `X-API-Key` = your key, `Content-Type` = `application/json`
   - Request Body: **JSON**, one field — key `url`, type Text, value
     **Shortcut Input**
4. Add nothing else. No "Show Result" action — the point is that it closes
   instantly and the answer arrives later as a notification.

Now: reel → Share → *Save Reel* → the sheet closes. Thirty seconds later the
takeaway lands on your lock screen.

## Reading your notes

- The DM reply itself — title, takeaways, the steps, the numbers.
- Notification → tap → the note (share-sheet path).
- `https://your-app/?k=<key>` — newest first, with search that covers titles,
  takeaways, steps, key facts and the full transcript. Bookmark it to your home
  screen. (Newest first is not a feature so much as a correction.)
- **Category chips** narrow to one topic in a tap, so looking for a finance note
  does not mean scrolling past travel. Only categories that actually have notes
  are offered, and a chip keeps whatever you have already typed in the search
  box — tapping one narrows, it never resets.
- `GET /api/notes?q=...&category=finance` with the `X-API-Key` header, if you
  want the JSON. `GET /api/categories` lists the categories in use with counts.

## Costs

Per reel: transcription is free on the local `faster-whisper` backend (slower on
a small box) or about $0.006/minute hosted; the Claude call runs a fraction of a
cent for a 60-second reel. The server is the only real line item.

Set `SAWIT_ASR=openai` with an `OPENAI_API_KEY` if CPU whisper is too slow
on your host — it is the one place this project talks to a non-Anthropic model,
because Claude does not do speech-to-text.

## Deliberately not built yet

Wait until the habit is proven before adding: a real job queue (background tasks
run in-process, which is fine for one user), multi-user auth, App Review for
other people's accounts, the Facebook Page equivalent of the DM path, and the
finance-reel calculator that turns `steps` into inputs you can edit. The open
question this is meant to answer is not "does it work" — it is whether you
actually read the replies.

## Tests

```bash
cd sawit && python -m pytest -q && python -m ruff check app tests
```

The suite covers storage and search, the extraction request shape, webhook
signature checking and payload parsing, delivery routing, and the HTTP surface.
It does not cover the yt-dlp download or a live Meta webhook — both depend on
Meta's servers, so they fail loudly at runtime instead.
