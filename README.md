<h1 align="center">Sawit</h1>

<p align="center"><em>Saw it for you.</em></p>

<p align="center">
  <strong>You already save the reel. Now you can read it.</strong><br>
  Share an Instagram reel &rarr; get the steps, the numbers and the caveats back as a
  searchable note, in about a minute.
</p>

<p align="center">
  <a href="https://prajwal2308.github.io/SAWIT/"><strong>Read the project page &rarr;</strong></a>
</p>

<p align="center">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="SQLite FTS5" src="https://img.shields.io/badge/SQLite-FTS5%20%2B%20vectors-003B57?logo=sqlite&logoColor=white">
  <img alt="faster-whisper" src="https://img.shields.io/badge/ASR-faster--whisper-5A29E4">
  <img alt="NVIDIA NIM" src="https://img.shields.io/badge/LLM-NVIDIA%20NIM-76B900?logo=nvidia&logoColor=white">
  <img alt="143 tests" src="https://img.shields.io/badge/tests-143%20passing-2ea44f">
  <img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue">
</p>

---

> **Status: finished, not maintained.** It works end to end and was deployed for
> a month &mdash; shared reels became searchable notes in about a minute.
> Development stopped because the honest answer to *"would I open this every
> day?"* turned out to be **no**: Instagram&rsquo;s own Saved folder is where
> people already look, and no amount of polish moves a habit that is not there.
> The code is public because the parts that were hard to get right &mdash;
> reading text off video frames, meaning-based search, per-account isolation,
> and eight production-only bugs &mdash; are worth more written down than
> deleted. Forks welcome; expect no roadmap.

## The problem

Instagram&rsquo;s Saved folder is a wall of thumbnails. **No search. No text. No
way to ask "what was that budget split?" six months later.** So people do the
next best thing &mdash; share reels to themselves in DMs &mdash; and end up with
a second pile they also cannot query.

The useful part of a reel is about forty seconds long. Sawit extracts it.

| | |
|---|---|
| **~60s** | share &rarr; finished note |
| **0s** | how long the share sheet waits on you |
| **$0** | marginal cost per reel |
| **0** | words a semantic query needs to share with the note it finds |

## What it actually produces

A reel with **no spoken audio at all**, whose content is a caption over music:

```json
{
  "title":    "Allocate monthly net income using a 55/5/10/15/15 split",
  "category": "finance",
  "one_liner":"Divide net income into 55% essentials, 5% guilt-free spending,
               10% debt or investing, 15% short-term savings, 15% long-term.",
  "steps":    ["Multiply monthly net income by 0.55 for essential expenses",
               "Multiply by 0.05 for guilt-free spending money",
               "Multiply by 0.1 and apply to debt, or invest it",
               "Multiply by 0.15 for short-term goals",
               "Multiply by 0.15 and invest for long-term wealth"],
  "key_facts":[{"label": "Essential expenses share", "value": "55%"},
               {"label": "Total allocation",         "value": "100%"}],
  "caveats":  ["Based on monthly net income after taxes, not gross"]
}
```

**The transcript for that reel was empty.** Every field above was read off the
video frames.

## The stack, and why each piece

| Layer | Choice | Why this one |
|---|---|---|
| **API** | FastAPI | Returns `202` in ~0s and does the work in a background task, so the iOS share sheet never waits |
| **Fetch** | yt-dlp | Handles Instagram, Facebook, TikTok and anything with a URL; falls back to the caption when a post is an image carousel |
| **Render** | ffmpeg / ffprobe | 16 kHz mono wav for Whisper, plus 8 JPEG stills sampled inside the clip |
| **ASR** | faster-whisper, `int8` on CPU | Free and local. `int8` is what makes it viable on a small box; serialised behind a lock because the model *is* the memory budget |
| **Extraction** | **`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`** via NVIDIA NIM | A 30B mixture-of-experts with ~3B active, multimodal, on a free tier. Chosen for one specific reason &mdash; see below |
| **Embeddings** | **`nvidia/nemotron-3-embed-1b`**, 2048-dim | Same endpoint and same key as extraction, so semantic search adds no second service |
| **Storage** | SQLite + FTS5 + float32 vectors | One file. No ORM, no vector database, no Redis |
| **Auth** | `hashlib.scrypt` + `hmac`, stdlib only | Nothing worth a dependency to do what the standard library already does correctly |
| **UI** | Server-rendered HTML | ~20 lines of inline JS. No build step |

### Why the model is not interchangeable

**It takes 12 images per request.** Both `llama-3.2-vision` models take exactly
**one**, and gemma-3 and phi-3-vision returned 404 on that endpoint. This is the
single most load-bearing decision in the project, because a reel puts its numbers
on screen *across the whole clip*:

| Frames sent | What came back |
|---|---|
| **1** &nbsp;(`llama-3.2-90b-vision`) | title `"Gamble King"`, summary `"Gamble King"`, tags `["Gamble King"]` |
| **8** &nbsp;(`nemotron-3-nano-omni`) | `"Slang wordplay — Stu(dies/died/dying) against Smo(king), Drin(king), Gamble(king)"`, takeaways empty, category `other` |

Same reel, same 14-character transcript. The only difference is how much of the
screen the model was allowed to see &mdash; and whether the prompt let it say
*there is nothing here.*


## Architecture

```
  iOS share sheet ──► Shortcut ──┐
                                  ├──► POST /ingest ──► 202 in ~0s, row written
  Instagram DM ──► webhook ──────┘                          │
                                                             │  (background task)
   ┌─────────────────────────────────────────────────────────┘
   │
   ├─ 1. FETCH      yt-dlp downloads the mp4  ·  no video? fall back to the caption
   ├─ 2. PROBE      ffprobe asks whether an audio stream exists at all
   ├─ 3. RENDER     ffmpeg → 16 kHz mono wav  +  8 stills at even offsets
   ├─ 4. TRANSCRIBE faster-whisper, int8 on CPU, one at a time behind a lock
   ├─ 5. EXTRACT    transcript + 8 base64 frames → multimodal model → typed note
   ├─ 6. EMBED      the note (not the transcript) → 2048-dim vector
   ├─ 7. STORE      SQLite: row + FTS5 index + vector + one thumbnail
   └─ 8. DELIVER    reply in the DM thread, or push via ntfy
                                                             │
   the temp directory is deleted here ──────────────────────┘
   video, audio and all 8 frames go with it
```

Everything after the 202 happens in a `tempfile.TemporaryDirectory`, so the
video is gone by the time the note exists — including when the pipeline throws.
What survives is the note, its transcript, one still, and a vector. 17 notes
occupy 36 MB; a single reel download is 8.8 MB, which is the whole argument for
not keeping them.

### How the extraction works

The model is asked for a **typed object, not prose**. `ReelNote` is a Pydantic
model — title, category, one-liner, takeaways, key facts, steps, caveats, tags —
and the same schema is enforced three different ways depending on what the
backend supports:

1. **Anthropic** — `messages.parse()` with the model class, which validates on
   the way out.
2. **OpenAI-compatible** — `response_format={"type": "json_schema", strict: true}`
   with the schema inlined. `$ref`/`$defs` are where open models' constrained
   decoding falls over, so `strict_schema()` resolves every reference, closes
   every object with `additionalProperties: false`, and marks every field
   required.
3. **Fallback** — if the endpoint rejects `json_schema` outright, it retries in
   plain JSON mode with the schema in the prompt. A weaker model degrades
   instead of failing the note.

Then `_parse` validates. If that fails, it scans brace-matched candidates and
takes the first that validates — because a *reasoning* model narrates before it
answers, and the note arrives after the prose rather than instead of it. Losing
a reel to a preamble is the wrong trade.

**The frames are usually the entire source.** Every reel tested transcribed to
nothing: they are music over on-screen text. The transcript was empty and the
note came from the stills. This is why the model choice is not interchangeable —
the default takes **12 images per request** and the llama-vision models take
exactly **one**. At one frame the extractor returned the words on a single card
as the title, the summary, and every tag. At eight it read a six-panel visual
pun correctly.

**The prompt permits emptiness.** Told to put "every number and name" in
`key_facts`, a model handed a joke reel obliges and emits rows whose label
restates their value. The rules now say an empty field is a correct answer, that
a key fact whose label repeats its value is on-screen text copied out rather
than a fact, and that a reel teaching nothing should reach for `other`.

### How search works

Two indexes over the same notes, because they answer different questions.

**FTS5** matches words — exact phrases you remember, ranked newest-first.

**Embeddings** match meaning. Each note gets a 2048-dimension vector from the
same endpoint that does the extraction, so this needs no second service and
nothing running locally. Similarity is cosine over a plain Python loop: a few
thousand notes is a few milliseconds, and it keeps numpy out of an image already
tight on memory. A relevance floor stops the nearest note being returned however
unrelated it is.

Results are merged keyword-first, so adding meaning never costs precision on
searches that already worked. What it buys:

| Query | Note returned | Words shared |
|---|---|---|
| how should i split my salary | Allocate monthly net income using a 55/5/10/15/15 split | 0 |
| something sweet to cook | Mango Sago Dessert Recipe | 0 |
| finding candidates to hire | Juicebox: build candidate shortlists and outreach | 0 |

The transcript is deliberately left out of the vector. It is long, usually empty
on these reels, and averaging it in drags the note toward whatever the creator
rambled about rather than what the note is for.

### How isolation works

SQLite has no row-level security, so it lives one level down. A `Store` is
**constructed bound to an account**, and every note query filters on that id
inside `store.py`. Endpoints cannot forget the filter because they never write
one — `get_store` is request-scoped, so all 20+ routes inherited isolation
without being edited. An unbound `Store` **raises** rather than quietly
returning everybody's notes, which is the failure mode that matters.

`tests/test_isolation.py` is what stands in for RLS. It drives the whole note
surface with two accounts, asserts that a note id from another account is
indistinguishable from one that does not exist (404, not 403), that writes
cannot reach across, that dedup does not leak the existence of someone else's
note — and it walks every `Store` method by reflection, failing if a newly added
one forgets to scope itself.

### Failure handling

Work runs in-process, so a redeploy kills whatever was mid-flight. Anything
still `pending` at boot is marked failed with a reason and a Retry button rather
than sitting on "working…" forever. Every stage degrades rather than cascading:
a missing frame is skipped, a failed embedding costs meaning-search for one note,
a rejected DM falls back to push, and a post with no video falls back to its
caption.

### Layout

| Module | Lines | Does |
|---|---|---|
| `main.py` | 1605 | HTTP surface, auth, and the server-rendered UI |
| `store.py` | 498 | SQLite, FTS5, vectors, and the account binding |
| `extract.py` | 282 | Prompt, structured output, and the salvage parser |
| `media.py` | 254 | yt-dlp, ffprobe, ffmpeg, caption fallback |
| `embed.py` | 166 | Vectors, cosine, and the relevance floor |
| `instagram.py` | 165 | Webhook signature checks and DM replies |
| `config.py` | 145 | Every knob, resolved once from the environment |
| `pipeline.py` | 141 | The eight steps above, in order |
| `shortcut.py` | 118 | Generates a per-account iOS shortcut |
| `accounts.py` | 107 | scrypt hashing and signed sessions, stdlib only |
| **tests** | **1994** | 143 of them, no network calls, ~8 seconds |

No framework beyond FastAPI, no ORM, no vector database, no Redis, no
JavaScript build step. The UI is server-rendered HTML with about twenty lines of
inline script.


## What broke in production

Eight bugs, six of which could not reproduce locally. They are the most useful
thing in this repo.

**`docker VOLUME is not supported`** — Railway rejects the directive in favour
of its own volumes. It was only a hint anyway; Fly mounts through `fly.toml`.

**`Output file does not contain any stream`** — ffmpeg asked for audio from a
video that had none. Instagram serves a **video-only stream to datacenter IPs**
where it gives a home connection both, so this failed in production and passed
locally *on the same URL*. Now it probes with ffprobe first and lets the frames
carry the note — which was already how most of these reels worked.

**A session cookie without `Secure`** — the cookie holding the API key shipped
unprotected. Railway terminates TLS at its proxy and forwards plain http, so
`request.url.scheme` read `http` inside the container and the flag was skipped.
Fixed by trusting `X-Forwarded-Proto` from the platform proxy. Locally the
scheme really *is* http, so the behaviour looked correct.

**Out of memory, three times in a row** — two reels retried together each built
their own `WhisperModel`. `lru_cache` does not serialise a concurrent miss, so
the cache was no protection, and the model *is* the memory. Transcription now
holds a lock across both the load and the decode, because `segments` is a
generator and releasing after the load would leave two decodes overlapping.

**`did not return a usable note`** — a reasoning model narrates before it
answers, so the JSON arrived after prose. The parser now scans brace-matched
candidates and takes the first that validates.

**12 notes for 8 reels** — no URL dedup. One reel had been downloaded,
transcribed and sent to the model **four separate times**. Re-sharing is how
people find a note again, not a request to redo it.

**Clearing the search did nothing** — the form carried the active category in a
hidden field, so emptying the filter you could see re-submitted the one you
could not. The page now states what it is filtered by, in words, with a Clear
that returns everything.

**`No video formats found`** — an image carousel has no video stream at all, so
yt-dlp refuses it outright and the share was lost. It falls back to the post's
caption, which on a ten-slide travel guide is where the content actually is.

The pattern worth extracting: **five of these are environment differences, not
logic errors.** Datacenter IP versus home IP, TLS terminated at a proxy versus
served directly, concurrent requests versus one at a time. None would have been
caught by more unit tests. They were caught by deploying and then driving the
real thing.


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

## Swapping the model

Two backends, set with `SAWIT_LLM`.

**`nvidia`** &mdash; any OpenAI-compatible endpoint. A free key from
[build.nvidia.com](https://build.nvidia.com) gets you open models at no cost:

```bash
SAWIT_LLM=nvidia
NVIDIA_API_KEY=nvapi-...
SAWIT_MODEL=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning   # the default
SAWIT_EMBED_MODEL=nvidia/nemotron-3-embed-1b                # the default
SAWIT_FRAMES=8                                              # check your model's image cap
```

`NVIDIA_BASE_URL` points anywhere OpenAI-compatible, so this backend is not
actually NVIDIA-specific &mdash; it is the shape of the API, not the vendor.

**`anthropic`** &mdash; the better extractor, noticeably so at reconstructing a
calculation into `steps` from a messy transcript. Needs `ANTHROPIC_API_KEY`.

**Before you swap, check two things.** How many images the model accepts per
request &mdash; one is not enough, and it is not usually documented, so send two
and see what happens. And whether it is a *reasoning* model, which will narrate
before answering; the salvage parser handles that, but a non-reasoning model
returns cleaner JSON to begin with.

Structured output is requested as a strict JSON schema with references inlined,
because `$ref`/`$defs` are where open models' constrained decoding falls over.
If a model rejects `json_schema` outright it retries in plain JSON mode with the
schema in the prompt, so a weaker model degrades instead of failing the note.

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

## What this project actually taught

**Five of the eight production bugs were environment differences, not logic
errors.** A datacenter IP gets a different Instagram response than a home
connection. TLS terminated at a proxy makes the app see plain http. Two
concurrent requests build two copies of a model that one request never
duplicates. None of these are findable by writing more unit tests; they are
findable by deploying and then driving the real thing.

**The prompt was as load-bearing as the model.** The same model, on the same
reel, went from emitting `Gamble(king): Gamble(king)` as a "key fact" to
correctly returning empty fields &mdash; because the instructions changed to say
that an empty field is a correct answer. Model capability was never the
bottleneck; permission to say nothing was.

**Enforce invariants where they cannot be forgotten.** Account isolation lives
in the constructor of `Store`, not in a check each endpoint remembers to write.
That is why adding routes did not add holes, and why a reflection test can
assert the property for methods that do not exist yet.

**The hardest problem was not technical.** Getting the pipeline right took a
day. Getting a person to press one button repeatedly is the part that did not
work, and no architecture fixes it. The Shortcut went from fifteen steps to two
taps and one paste; the honest read is that even two taps is more friction than
a habit that already exists elsewhere.

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
ruff check app tests && pytest -q
```

143 tests, no network calls, about eight seconds. They cover storage, both
search paths, the extraction request shape, webhook signature checking,
delivery routing, restart recovery, account isolation, password hashing and
session signing, the generated shortcut, and the HTTP surface.

Three are worth reading on their own:

- **`test_isolation.py`** stands in for row-level security. It drives the whole
  note surface with two accounts, and walks every `Store` method by reflection
  to fail if a newly added one forgets to scope itself.
- **`test_transcribe.py`** runs four threads through the local Whisper path and
  asserts none of them overlap — the regression test for the OOM.
- **`test_schema_contract.py`** runs the Anthropic SDK's own schema transform
  locally, so an SDK upgrade that would break extraction fails in CI rather
  than against a live reel.

It does not cover the yt-dlp download, a live Meta webhook, or a real model
call — those depend on someone else's servers, so they fail loudly at runtime
instead. Which is exactly why six of the eight production bugs above needed a
deploy to find: they were environment differences, not logic errors.
