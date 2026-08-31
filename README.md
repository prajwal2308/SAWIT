# Sawit

*Saw it for you.*

Send a reel to it. Get the point back. Never rewatch.

> **Status: finished, not maintained.** It works end to end and is deployed —
> shared reels become searchable notes in about a minute. Development stopped
> because the honest answer to "would I open this every day?" turned out to be
> no: Instagram's own Saved folder is where people already look, and no amount
> of polish moves a habit that is not there. The code is here because the parts
> that were hard to get right — reading text off frames, meaning-based search,
> per-account isolation, and eight production-only bugs — are worth more written
> down than deleted. Issues and forks welcome; expect no roadmap.

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
               ffmpeg ───────────┤ 16 kHz mono audio + 8 stills
               whisper ──────────┤ transcript
               a model ──────────┤ typed note (title, takeaways,
                                 │   steps, key facts, caveats)
               SQLite + FTS5 ────┤ stored and searchable
                                 └─► reply in the DM thread, or push via ntfy
```

Two design decisions worth knowing:

**The stills are not decoration — they are usually the whole source.** Reels
routinely put the numbers, formulas and lists on screen and never say them out
loud. Eight frames go to the model alongside the transcript, so a "how to
calculate X" reel yields the actual calculation. In practice most reels tested
had no speech at all, and the transcript was empty.

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

## Which model writes the notes

Two backends, set with `SAWIT_LLM`.

**`anthropic`** (default) — the better extractor, noticeably so at the one job
that matters most here: reconstructing a calculation into `steps` from a messy
transcript. Needs `ANTHROPIC_API_KEY`.

**`nvidia`** — any OpenAI-compatible endpoint. A free key from
[build.nvidia.com](https://build.nvidia.com) (starts with `nvapi-`) gets you
open models on NVIDIA's hosted inference at no cost:

```bash
SAWIT_LLM=nvidia
NVIDIA_API_KEY=nvapi-...
SAWIT_MODEL=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning   # the default
```

**Pick a multimodal model, and check how many images it takes.** Frames go to
the model alongside the transcript because reels put the numbers on screen and
never say them out loud — in testing, *every* reel transcribed to nothing and
the notes were written entirely from the frames. The default is chosen because
it accepts **12 images per request**; both `llama-3.2-vision` models accept
exactly one, which is not enough to read a reel whose text changes as it plays.
At one frame the extractor returned the words on a single card as the title, the
summary and every tag. If you deliberately choose a text-only model, set
`SAWIT_VISION=false` so it is not sent images it cannot read.

Structured output is requested as a strict JSON schema, with references inlined
because that is where open models' constrained decoding tends to fall over. If a
model rejects `json_schema` outright, it retries in plain JSON mode with the
schema in the prompt — so a weaker model degrades instead of failing the note.

`NVIDIA_BASE_URL` points anywhere OpenAI-compatible, so this backend is not
actually NVIDIA-specific.

**Expect a quality gap.** Open models handle the summary fine. Where they lose
to Claude is the calculation: getting every step, in order, with the right
numbers. That is the field this whole app exists for, so it is worth re-checking
a few finance reels by hand before trusting it.

## Install

You need **Python 3.12+**, **ffmpeg** on `PATH`, and a free API key from
[build.nvidia.com](https://build.nvidia.com) (starts with `nvapi-`). About five
minutes.

```bash
git clone https://github.com/prajwal2308/SAWIT.git
cd SAWIT
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

No ffmpeg? `brew install ffmpeg` on macOS, `sudo apt install ffmpeg` on Debian
or Ubuntu. The service checks for it and says so if it is missing.

Then write a `.env`:

```bash
cp .env.example .env
python -c "import secrets; print('SAWIT_API_KEY=' + secrets.token_urlsafe(32))"
```

Put that key in `.env`, add your `NVIDIA_API_KEY`, and set `SAWIT_LLM=nvidia`.
Everything else has a working default — the model, the embedding model, frame
count and transcription are all already set to values that work together, and
the notes on each are in `.env.example` if you want to change them.

```bash
uvicorn app.main:app --reload
```

Open `http://localhost:8000/?k=<your SAWIT_API_KEY>`. That first visit trades
the key for a cookie, so you only paste it once. Paste a reel link into the
**Add** tab, and about a minute later you have a note.

`SAWIT_API_KEY` is required — the service refuses to start without one, and
becomes the first account's key. This endpoint downloads and transcribes
whatever URL it is handed, so an unauthenticated instance is somebody else's
free compute.

### Check it is wired up

```bash
curl -H "X-API-Key: $SAWIT_API_KEY" localhost:8000/api/status
```

That reports whether ffmpeg is present, which model and ASR backend are
selected, whether the LLM key is set, and note counts by state. It is the
fastest way to find the one variable you forgot.

### Running the tests

```bash
pip install pytest ruff
ruff check app tests && pytest -q
```

143 tests, no network calls, a couple of seconds. `tests/test_isolation.py` is
the one worth reading first — it is what stands in for row-level security.

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

## Cookies — try without them first

**Start with `YTDLP_COOKIES_FILE` empty.** Public reels download unauthenticated,
and if yours do, you are done: there is no credential to leak, nothing to rotate,
and nothing to put on a server.

Reach for cookies only when a download actually fails with a login wall — private
accounts, age-restricted posts, or an IP that Instagram has started throttling.

When you do need them, **use a throwaway Instagram account, not your own.** A
`cookies.txt` is not password-shaped, it is session-shaped: `sessionid` is a live
login, and whoever holds the file is you — your DMs, your posts, everything. That
matters more than it first appears, because the file has to travel:

- **Deployed**, it lives on a host you do not fully control, in a volume or an
  image layer, for as long as the session lasts.
- **Shared with anyone else**, every reel *they* save is downloaded as *you*.
  One account pulling hundreds of reels from a datacenter IP is the shape of
  traffic that gets accounts disabled — a ban risk, not just a privacy one.

If both of those apply to you, the DM path is the real answer rather than a
better cookie jar: Meta hands over the media URL itself, so there is no scraping
and no credential anywhere.

Export in Netscape format (any "cookies.txt" exporter extension), keep it out of
git — the shipped `.gitignore` already covers `cookies.txt` — and point
`YTDLP_COOKIES_FILE` at it.

## Run it from your laptop first

You do not need a host to use this. A tunnel gives your laptop a public HTTPS
URL, which is all the Shortcut needs:

```bash
uvicorn app.main:app --port 8000            # one terminal
cloudflared tunnel --url http://localhost:8000   # another; prints an https URL
```

Point the Shortcut at that URL and share a reel. Transcription runs locally and
free, extraction runs on NVIDIA's free tier, and the total cost is nothing.

The catch is that it only works while the laptop is awake — share a reel while
it is closed and nothing happens until you re-share. For finding out whether
you actually read the notes, that is a fine trade, and it beats spending a
weekend on hosting for a habit you have not proven yet.

Note that the free `cloudflared` URL changes every restart, so re-point the
Shortcut each session. A named tunnel on a domain you own keeps it stable.

**The DM path cannot work this way.** Meta needs a webhook URL that is
reachable whenever someone shares, and it retries against a dead host. Do the
Shortcut on a tunnel first; set up the DM path once you have somewhere
permanent to put it.

## Deploy

Worth doing once the habit is proven, not before.

`fly.toml` is committed and already has the volume mount, the health check and
a machine big enough for CPU whisper. `Dockerfile` installs ffmpeg and serves on
`$PORT`, so Railway and Render work off it too.

```bash
fly launch --no-deploy --copy-config      # keeps the committed fly.toml
fly volumes create sawit_data --size 1    # notes must outlive a redeploy
fly secrets set \
  SAWIT_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  ANTHROPIC_API_KEY=sk-ant-... \
  NTFY_TOPIC=sawit-something-long-and-random
fly deploy
fly secrets set PUBLIC_BASE_URL=https://<your-app>.fly.dev   # makes pushes tappable
```

Two settings in `fly.toml` are deliberate, not defaults: `auto_stop_machines`
is off because stopping a machine mid-transcription loses the note, and the VM
is 2 GB because faster-whisper will OOM below that.

### Railway

`railway.json` is committed and points at the same `Dockerfile`, so the build
needs no configuration. Two things do:

**Add a volume before the first deploy**, mounted at `/data`. Railway's
filesystem is ephemeral — without one, every redeploy silently wipes your notes.
Then set `SAWIT_DB=/data/sawit.sqlite3` to match the mount.

**Keep it to one replica.** Work runs in-process and the store is a single
SQLite file, so a second replica means background tasks that nothing tracks and
two writers on one database. `numReplicas` is 1 in `railway.json` for that
reason.

```bash
railway login
railway init
# Add the volume at /data in the dashboard, then set the variables:
railway variables --set SAWIT_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
                  --set SAWIT_DB=/data/sawit.sqlite3 \
                  --set SAWIT_LLM=nvidia \
                  --set NVIDIA_API_KEY=nvapi-... \
                  --set NTFY_TOPIC=sawit-something-long-and-random
railway up
railway variables --set PUBLIC_BASE_URL=https://<your-app>.up.railway.app
```

Memory is the thing to watch: local `faster-whisper` wants ~2 GB. If the deploy
OOMs, `SAWIT_ASR=hosted` moves transcription off the box and the service becomes
small enough for the cheapest tier.

### Check it came up

```bash
curl -H "X-API-Key: $SAWIT_API_KEY" https://<your-app>.fly.dev/api/status
```

That reports what is actually wired up — ffmpeg, the ASR backend, whether
Instagram DMs and push are configured, and note counts by state. It is the
fastest way to find the one env var you forgot.

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

## Accounts

Notes belong to accounts. **/signup** creates one, **/login** returns to it, and
**/account** shows the API key that account's Shortcut should send.

SQLite has no row-level security, so the isolation lives one level down: a
`Store` is constructed bound to an account and every note query filters on it
inside `store.py`. Endpoints cannot forget the filter because they never write
one, and an unbound `Store` raises rather than quietly returning everybody's
notes. `tests/test_isolation.py` is what stands in for RLS — it drives the whole
note surface with two accounts, and walks every `Store` method to fail if a new
one forgets to scope itself. A note id from another account returns 404 rather
than 403: whether it exists is not something to leak.

**Upgrading from before accounts existed costs nothing.** On first boot with no
accounts, `SAWIT_API_KEY` becomes the first account's key and every note that
predates accounts is handed to it, so an existing Shortcut keeps working and no
library disappears.

**Signup is open.** Anyone who has the URL can create an account, and their
transcription and model calls come out of your quota. Put the address somewhere
private, or add an invite gate, before sharing it.

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
- **Search understands what you meant**, not only what you typed. Every note
  carries an embedding alongside the FTS index, so "how should I split my
  salary" finds the note titled "Allocate monthly net income using a
  55/5/10/15/15 split" — which shares no word with the query. Keyword hits still
  rank first, so exact phrases you remember behave as they always did. The
  vectors come from the same endpoint as the extraction, so this needs no second
  key and nothing running locally; `POST /api/reindex` backfills notes written
  before it existed.
- **The feed** (`/feed`) pages sideways, one note per card, with the note
  scrolling down inside it — two axes doing two jobs, so no gesture has to guess
  which you meant.
- `GET /api/notes?q=...&category=finance` with the `X-API-Key` header, if you
  want the JSON. `GET /api/categories` lists the categories in use with counts.

## Costs

Per reel: transcription is free on the local `faster-whisper` backend (slower on
a small box) or about $0.006/minute hosted; the Claude call runs a fraction of a
cent for a 60-second reel. The server is the only real line item.

Set `SAWIT_ASR=openai` with an `OPENAI_API_KEY` if CPU whisper is too slow
on your host — it is the one place this project talks to a non-Anthropic model,
because Claude does not do speech-to-text.

## When something goes wrong

**An image post, not a reel.** A carousel has no video stream, so yt-dlp
refuses it outright. Sawit falls back to the post's caption, which on a
ten-slide guide is usually where the content is. The slides themselves are
behind Instagram's login wall and are not read.

**A reel you already saved.** Re-sharing returns the note you already have
rather than downloading and transcribing it a second time. A *failed* note is
not a hit — re-sharing that does try again.

**A note says "failed".** Open it — the error is on the page, and so is a
**Retry** button. Retry re-runs through the page URL, so it works for anything
shared as a link. A reel that arrived as a DM attachment cannot be retried
after Meta's CDN link expires; the app says so and asks you to re-share it.

**A note is stuck on "working…".** It is not. Work runs in-process, so a
redeploy or crash kills whatever was mid-flight; on the next boot those notes
are marked failed with "Interrupted by a restart" and can be retried. Nothing
sits in limbo.

**Deleting.** Every note page has a Delete button, and `DELETE /api/notes/{id}`
does the same thing. Deleting drops the note from search too.

**The first transcription is slow.** faster-whisper downloads its model on
first use (a few hundred MB). Subsequent runs are much faster.

**The host is too small for whisper.** Local transcription is the only reason
this needs a 2 GB machine. Move it off-box and the service becomes light enough
for almost any free tier:

```bash
SAWIT_ASR=hosted
ASR_BASE_URL=https://api.openai.com/v1     # or any OpenAI-compatible endpoint
ASR_API_KEY=...
ASR_MODEL=whisper-1
```

Nothing else changes, and `fly.toml` can then drop to `shared-cpu-1x` / 512 MB.

## Deliberately not built yet

Wait until the habit is proven before adding: a real job queue (background tasks
run in-process, which is fine for one user and is why the restart recovery
above exists), multi-user auth, App Review for other people's accounts, the
Facebook Page equivalent of the DM path, and the finance-reel calculator that
turns `steps` into inputs you can edit. The open question this is meant to
answer is not "does it work" — it is whether you actually read the replies.

The nine categories in `app/schemas.py` are a guess. If your reels cluster
somewhere that is not on that list, they will land in `other`; add the category
once you can see the pattern rather than guessing at it now.

## Tests

```bash
cd sawit && python -m pytest -q && python -m ruff check app tests
```

The suite covers storage, search and category filtering, the extraction request
shape, webhook signature checking and payload parsing, delivery routing,
restart recovery, and the HTTP surface. `test_schema_contract.py` runs the
Anthropic SDK's own schema transform locally, so an SDK upgrade that would
break extraction fails in CI rather than against a live reel.

It does not cover the yt-dlp download, a live Meta webhook, or a real Claude
call — those depend on someone else's servers, so they fail loudly at runtime
instead.
