# ticket-parser

WhatsApp bug reports → a categorised PDF, **one page per issue**, each page
carrying an LLM verdict on whether that issue actually exists in your code.

Single user, own machine. No web UI, no auth, no mail server, no database
server, no cloud account.

## How it works

```
Firefox extension        ticket-parser (Python)
scrape the open     →    import → collect → enrich → investigate → render
conversation             into     quiet-    cluster   confront the   one issue
(reads the DOM)          inbox/   period    into      repository     per page
                                  batching  issues    (claude)       (Chromium)
        ↓                    ↓        ↓         ↓           ↓            ↓
 Downloads/ticket-parser/  data/inbox/     data/done/               data/out/*.pdf
```

1. **extension** — you open your product owner's chat in Firefox and click
   *Export*. It scrolls back, reads the rendered messages, pulls the screenshots
   and voice notes straight out of the page, and writes one directory per
   message.
2. **import** — lifts those into `data/inbox/`.
3. **collect** — messages accumulate; a burst closes into a batch once the chat
   has been silent for `QUIET_PERIOD_SECONDS` (default 2 h).
4. **transcribe** — every voice note in the batch is run through a local,
   offline Whisper model (`faster-whisper`, no API key). The transcript is
   written back into the message's directory alongside the audio before
   anything else happens, so it survives crash recovery and is never
   recomputed on a re-run.
5. **enrich** — one `claude` call groups the messages into distinct issues and
   labels each with a title, category and severity. **Metadata only** — see
   *Verbatim* below.
6. **investigate** — one `claude` call per issue, run inside `REPO_PATH` with a
   read-only tool allowlist, producing a verdict, cited `file:line` evidence and
   a suggested fix.
7. **render** — Jinja2 → Chromium. One page per issue: title, tags, the messages
   verbatim with their screenshots and voice-note transcripts, and the code
   verdict at the bottom.

### Why the browser extension

Every other route was tried and failed on real accounts:

| Route | Outcome |
| --- | --- |
| Share selected messages to email | WhatsApp removed multi-select → Share |
| Export chat with media | 132 MB per export, and re-sends the whole history |
| `whatsapp-web.js` linked device | Receives text, but `downloadMedia`, `getChats` and history all fail on current builds — screenshots are half a bug report |
| Baileys on a second number | Works, but needs a spare SIM and carries ToS risk |
| Screenshots + OCR | Destroys the verbatim guarantee the design rests on |

The extension reads what the page has **already decrypted**: images and voice
notes are `blob:` URLs WhatsApp Web itself created, so no keys and no protocol
knowledge are needed. It never calls WhatsApp's own JavaScript, so their
internals can churn without breaking it — only the DOM shape matters. And it is
your own browser and your own session, so there is no automation library to
fingerprint.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,transcribe]'   # drop ",transcribe" to skip voice-note support
.venv/bin/playwright install chromium

cp .env.example .env      # set REPO_PATH at minimum
```

### Loading the extension in Firefox

1. Go to `about:debugging#/runtime/this-firefox`.
2. **Load Temporary Add-on…** and pick `extension/manifest.json`.
3. Open `web.whatsapp.com` and log in (Settings → Linked devices on your phone).

A temporary add-on is unloaded when Firefox restarts; re-add it in two clicks,
or sign it through addons.mozilla.org if you want it permanent.

**Turn off the download prompt**, or you will be asked to confirm every file:
*Settings → General → Downloads → "Save files to…"* rather than "Always ask".

## Usage

Open your product owner's chat, click the extension button, choose how far to
scroll back, hit **Export**. Then:

```bash
ticket-parser import --then-run
```

That imports what the extension wrote, closes the batch, and renders the PDF in
one step. `ticket-parser import` alone stages the messages and leaves the
report for later.

Re-exporting the same conversation is safe at three levels: the extension writes
one directory per message id, `import` skips ids already present, and messages
already triaged in an earlier report are filtered out by content fingerprint. You
cannot double-report a bug.

### What the extension will not do

- **It cannot send.** It reads the DOM and fetches already-rendered images.
  There is no code path that types, clicks send, or modifies the page.
- **It only reads the chat you have open.** Nothing else in your WhatsApp is
  touched.
- **It has no network permission.** Files go to your downloads folder; nothing
  leaves the machine except the `claude` calls you configured.

### Attachments

**Screenshots** are read from the rendered `<img>`. Two routes are tried: a
direct fetch of the `blob:` URL, then — because Firefox scopes blob URLs to the
page's principal and a content script cannot fetch them — drawing the decoded
image onto a canvas and reading the pixels back. The canvas route re-encodes as
JPEG, so it is visually identical but not byte-identical to what WhatsApp holds.
The popup reports which route was used.

Only images WhatsApp has actually **rendered** exist in the page, so scroll
through the pictures you want before exporting.

**Documents** (`.docx`, `.pdf`, `.xlsx`, …) cannot be extracted: WhatsApp only
decrypts them when you click download, so there is nothing in the page to read.
They are detected by filename and named on the page — *"document attached:
cahier-des-charges.docx"* — plus flagged on the cover, so you know one exists
and can fetch it by hand. A message with a document is never rendered as if it
were empty.

### Timestamps

Text messages carry `data-pre-plain-text` (`[19:08, 13/08/2026] Brice:`), so
their time and sender are read directly. **Media rows do not** — they show only
a bare `19:08`. For those, the date is carried forward from the previous message
and combined with the visible time, then flagged: the report says how many
timestamps were inferred, and warns that a date may be wrong across a midnight
boundary.

### If a scrape comes back empty

WhatsApp restyles from time to time. Everything version-fragile is in one
`SELECTORS` block at the top of `extension/content.js`. To check what still
matches, open the chat, press F12 and run:

```js
console.log('rows:', document.querySelectorAll('[data-id]').length);
console.log('with metadata:', document.querySelectorAll('[data-pre-plain-text]').length);
console.log('blob images:', document.querySelectorAll('img[src^="blob:"]').length);
```

Non-zero row and image counts mean the scrape is fixable by adjusting that block
alone.

### Hands-off mode

```bash
ticket-parser watch
```

Polls the drop folder every `WATCH_INTERVAL_SECONDS` (default 30) and renders a
report as soon as a batch closes. Then the whole workflow is: click Export in
Firefox, and a PDF appears in `data/out/` once the quiet period elapses. One bad
batch logs an error and the watcher keeps going.

### Triage before spending money

When you run `flush` from a terminal, each issue is shown with the messages
behind it and you keep or discard it:

```
────────────────────────────────────────────────────────────────
Issue 2/3 · process · severity unknown
Question about the deploy schedule

  [13/08 15:51] Emmanuel DE: Au fait, on déploie quand la prochaine version ?

Keep? [Y/n/a=keep all/x=discard all/?]
```

Only kept issues are investigated, and an investigation is a full `claude`
session — so discarding your product owner's questions saves both money and a
page of noise. Discarded issues still count as triaged: their messages are
marked seen and will not reappear in the next report, and the cover page lists
what was dropped.

Enter keeps, `n` discards, `a` keeps everything remaining, `x` discards
everything remaining. If input closes mid-review, everything left is kept.

Triage is on by default for `flush` on a terminal only. `run`, `watch`, cron
and `--quiet` never prompt — a question nobody can answer would hang the
pipeline. Force it either way with `--review` / `--no-review`.

### Mobile capture (optional)

The same extension, signed and installed on Firefox for Android:

```bash
python3 scripts/build_extension.py       # → dist/*.xpi (unsigned)
npx web-ext sign --channel=unlisted --source-dir=extension \
  --api-key=$AMO_JWT_ISSUER --api-secret=$AMO_JWT_SECRET
```

Firefox Android only installs Mozilla-signed extensions, so the unsigned build
is desktop-only. On the phone, open WhatsApp Web with *Request desktop site*
(link with a phone-number code rather than a QR, since you cannot scan your own
screen), read the conversation, then **Send bundle…** and share it to your mail
app.

To collect those, point the mailbox settings at the address you send to and set
`MAIL_ENABLED=true`. Then `watch` polls it each cycle, or fetch on demand:

```bash
ticket-parser mail --then-run
```

Mail is never deleted from the server, only mail from `ALLOWED_SENDERS` is read,
and `Message-ID` dedupe means re-polling is a no-op.

### All commands

```bash
ticket-parser watch                  # poll drop folder (+ mailbox), render as batches close
ticket-parser mail                   # fetch bundles attached to mail
ticket-parser import                 # pull what the extension exported
ticket-parser import --then-run      # …and render the report immediately
ticket-parser import --from PATH     # non-default drop folder
ticket-parser flush                  # close and render right now (triage prompt on a terminal)
ticket-parser flush --no-review      # …without the triage prompt
ticket-parser run                    # render only if the chat has gone quiet
ticket-parser status                 # what is pending, and when it will close
ticket-parser prune                  # drop done/ batches past RETENTION_DAYS
ticket-parser run --dry-run          # every stage, no PDF, commit nothing
ticket-parser run --html /tmp/r.html # dump the HTML too, for restyling
```

Every command exits non-zero on failure. `status` also exits non-zero when the
pending backlog exceeds `BACKLOG_WARN_THRESHOLD` — that means the pipeline has
been failing, not that you have a lot of bugs.

There is no daemon and nothing to keep alive: export when your product owner has
reported something, then import. If you would rather have the import happen on a
schedule, cron it — it is a no-op when the drop folder is empty:

```cron
*/10 * * * * cd /home/scorbutics/dev/ticket-parser && .venv/bin/ticket-parser import --then-run >> data/cron.log 2>&1
17 3 * * *   cd /home/scorbutics/dev/ticket-parser && .venv/bin/ticket-parser prune >> data/cron.log 2>&1
```

## Verbatim is structural, not a prompt instruction

The model **never emits message text**. It receives the messages with stable
integer ids and replies with JSON metadata: which ids group together, and a
title/category/severity for each group. The PDF renders every message body from
`ChatMessage.text` — the exported record itself — so there is no channel through
which a model could alter your product owner's wording.

Enforced by tests in `tests/test_render_verbatim.py`, including one that feeds
the clusterer a hostile reply containing rewritten text and asserts none of it
reaches the document.

Related guarantees:

- An id the model invents is **dropped and reported** in the run banner.
- A message the model **forgets** lands in a catch-all "Uncategorised messages"
  issue, so no content ever disappears from the report.
- Autoescaping is on unconditionally, so the one place model prose *is* rendered
  — the verdict paragraph — cannot inject markup.

## The repository pass

Each issue gets its own `claude` session with `cwd` pinned to `REPO_PATH` and
this allowlist: `Read`, `Grep`, `Glob`, and read-only git (`log`, `show`,
`blame`, `diff`, `status`, `rev-parse`). `Edit`, `Write`, `WebFetch` and
`WebSearch` are explicitly denied. **The pipeline never mutates your repo.**

Verdicts are cached in SQLite on `(issue content, repo git HEAD, prompt version,
model)`, so re-rendering costs nothing, while a new commit — or a change of
model — invalidates the verdict rather than serving a stale one. Failures are
never cached.

### Choosing models per pass

The two passes are different jobs, so they are configured separately:

| Setting | Job | Reasonable choice |
| --- | --- | --- |
| `CLAUDE_MODEL_CLUSTER` | Read a handful of messages, return a few lines of JSON | `claude-haiku-4-5-20251001` |
| `CLAUDE_MODEL_INVESTIGATE` | Navigate an unfamiliar codebase, reach a judgement you will act on | `claude-opus-5` |
| `CLAUDE_MODEL` | Fallback when either is unset | `claude-sonnet-5` |

Leaving all three empty defers to whatever the `claude` CLI defaults to. Since
the model is in the cache key, you can change one and re-run to compare verdicts
on the same issue without hand-clearing anything.

The verdict is `confirmed`, `not_found` or `inconclusive`, with a confidence, a
short paragraph of observations, cited `file:line` evidence and a suggested fix.
Every page footnotes the commit it was analysed against and says to verify
before acting — it is an automated opinion, not a review.

## Failure policy

An LLM failure never costs you the document:

| What fails | What you get |
| --- | --- |
| `claude` missing or `LLM_ENABLED=false` | One flat page-set in chat order, banner explaining why |
| Clustering call fails or returns junk | Same as above |
| One issue's repo pass fails | That page renders without a verdict; the others keep theirs |
| No `REPO_PATH` configured | Issue pages, no verdict section |

The only hard failure is an unwritable PDF.

## Storage

```
data/
  inbox/       imported messages awaiting a batch
  staging/     in-progress work (crash recovery)
  done/        committed batches, pruned after RETENTION_DAYS (default 30)
  out/         generated PDFs, never auto-pruned
```

Plus `~/Downloads/ticket-parser/`, where the extension drops its export and
`import` consumes it. Everything is staged in a hidden `.partial` directory and
made visible with a single `os.rename`, so a crash leaves either the old state
or the new one, never a half-written message.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Export finds 0 messages | WhatsApp restyled. Run the three-line probe above; fix the `SELECTORS` block in `extension/content.js` |
| Firefox asks where to save every file | Settings → General → Downloads → "Save files to…", not "Always ask" |
| Extension gone after restarting Firefox | Temporary add-ons are unloaded on restart. Re-add via `about:debugging`, or get it signed |
| Screenshots missing from the report | The page had not rendered them yet. Scroll through the images in WhatsApp first, then export |
| Timestamps look wrong on image messages | Media rows carry no date; it is carried forward from the previous message. The report flags how many were inferred |
| "nothing to do" but messages were imported | They were already triaged in an earlier report. Dedupe is on message content |
| Nothing happens after import | The quiet period hasn't elapsed. `status` shows the countdown; `flush` or `import --then-run` overrides it |
| `claude not found on PATH` | Set `CLAUDE_BIN` to the absolute path — cron's PATH is not your shell's |
| A verdict looks stale | It was cached against an older HEAD; commit and re-run to invalidate |
| Playwright fails to launch Chromium | Missing system libs: `.venv/bin/playwright install-deps chromium` |

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Legacy

`src/ticket_parser/ingest.py` is a complete, tested IMAP/GMX mail ingestion
path from an earlier design. It still works and costs nothing to keep, but is
not wired into the CLI. Say the word if you want it gone too.

An earlier `whatsapp-web.js` sidecar has been removed: on current WhatsApp Web
builds it could receive text but not media or history, which the browser
extension does properly. The project no longer depends on Node at all.
