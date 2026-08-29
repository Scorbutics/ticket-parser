'use strict';

/**
 * Scrapes the open WhatsApp Web conversation.
 *
 * Why this works where whatsapp-web.js does not: we read what the page has
 * already rendered and decrypted. Images are `blob:` URLs the page created
 * itself, so fetching them needs no keys and no protocol knowledge. Nothing
 * here calls into WhatsApp's own JavaScript, so their internals can change
 * without breaking us -- only the DOM shape matters.
 *
 * This script reads. It never types, clicks send, or modifies the page.
 *
 * The one exception to "just read the DOM" is voice notes: WhatsApp never
 * puts one in the DOM as an <audio> element, so there is nothing there to
 * read directly. See page-hook.js for how the audio itself is captured
 * instead -- a hook on a standard Web API, not a WhatsApp internal.
 *
 * A second, related wrinkle: WhatsApp's chat list is virtualized, capping how
 * many `[data-id]` rows are mounted at once (observed at exactly 100,
 * suspiciously round). Once a conversation exceeds that, further scrolling
 * doesn't grow the live row count at all -- older rows get evicted from the
 * DOM as new ones are prepended, so a *single* scrape at the end would only
 * ever see whatever happens to still be mounted then. Every row's full data
 * (not just its voice-note duration) is therefore captured the moment it's
 * seen, during scrolling, into a record set that only ever grows -- see
 * captureEverything and messageRecords below.
 */

// ---------------------------------------------------------------------------
// Everything version-fragile lives here. If WhatsApp restyles and the scrape
// comes back empty, this block is the only thing that should need changing.

const SELECTORS = {
  // Each message row carries data-id like "false_<chat>@c.us_<messageId>".
  row: '[data-id]',
  // Holds data-pre-plain-text: "[09:12, 15/01/2024] Emmanuel DE: "
  meta: '[data-pre-plain-text]',
  // The message text itself.
  text: '.selectable-text',
  // Decrypted media the page has already rendered.
  image: 'img[src^="blob:"]',
  // A voice note's progress bar. Only present while a row is close enough to
  // the viewport to be fully mounted (WhatsApp's chat list is virtualized --
  // see captureEverything for why that matters). `aria-valuetext=
  // "0:00/1:26"` on it is what a row is matched to captured audio by.
  //
  // `role="slider"` alone is not exclusive to voice notes -- a video
  // message's scrubber uses it too, and a video's thumbnail can read as an
  // image via SELECTORS.image, so without this gate a video row could be
  // wrongly treated as a voice note whose audio can never be found (there is
  // no audio/* blob for a video's scrubber). `aria-label="Voice message"` is
  // a plain, always-rendered-alongside-the-slider label specific to voice
  // notes, so it gates that false-positive out.
  voiceNoteLabel: '[aria-label="Voice message"]',
  voiceSlider: '[role="slider"]',
  // The conversation pane; used to find the scroll container.
  main: '#main',
};

const SCROLL_SETTLE_MS = 900;
// Each voice note's audio is decoded (for its duration) asynchronously as
// page-hook.js captures it; give any decode still in flight from the last
// scroll pass time to land before reading back what it found.
const AUDIO_SETTLE_MS = 1500;

// ---------------------------------------------------------------------------

/**
 * Read back what page-hook.js has captured so far.
 *
 * `window.wrappedJSObject` is Firefox's escape hatch for an isolated-world
 * content script to reach a plain value a page script set directly on
 * `window` -- exchanging a JSON string, rather than the array of objects
 * itself, sidesteps the Xray wrapper restrictions on structured data.
 */
function readCapturedAudioLog() {
  try {
    const raw = window.wrappedJSObject && window.wrappedJSObject.__ticketParserAudioLogJSON;
    return raw ? JSON.parse(raw) : [];
  } catch (err) {
    return [];
  }
}

function parseClockToSeconds(clock) {
  const match = String(clock || '').match(/^(\d+):(\d{2})$/);
  if (!match) return null;
  return Number(match[1]) * 60 + Number(match[2]);
}

/**
 * A voice note's total duration, read from its progress slider's
 * `aria-valuetext` ("0:00/1:26") -- the field a row is matched to captured
 * audio by, since nothing else in the DOM ties the two together.
 */
function voiceNoteDurationSeconds(row) {
  const slider = row.querySelector(SELECTORS.voiceSlider);
  if (!slider) return null;
  const valueText = slider.getAttribute('aria-valuetext') || '';
  return parseClockToSeconds(valueText.split('/')[1]);
}

function findScrollContainer() {
  const main = document.querySelector(SELECTORS.main);
  if (!main) return null;
  const row = main.querySelector(SELECTORS.row);
  let node = row ? row.parentElement : main;
  while (node && node !== document.body) {
    const style = window.getComputedStyle(node);
    const scrollable = /auto|scroll/.test(style.overflowY);
    if (scrollable && node.scrollHeight > node.clientHeight + 50) return node;
    node = node.parentElement;
  }
  return main;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * "[09:12, 15/01/2024] Emmanuel DE: " -> { timestamp, sender, raw }
 *
 * Day/month order is locale-dependent and genuinely ambiguous for days <= 12,
 * so we disambiguate with the browser locale and always keep the raw string,
 * which the report can fall back on.
 */
function parseMeta(raw) {
  const result = { timestamp: null, sender: '', raw: raw || '' };
  if (!raw) return result;

  const match = raw.match(/^\[(.+?)\]\s*(.*?):\s*$/);
  if (!match) return result;
  result.sender = match[2].trim();

  const stamp = match[1].trim();
  const parts = stamp.match(
    /(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]\.?m\.?)?[,\s]+(\d{1,2})\/(\d{1,2})\/(\d{2,4})/i
  );
  if (!parts) return result;

  let [, hour, minute, second, meridiem, first, secondNum, year] = parts;
  hour = Number(hour);
  if (meridiem) {
    const isPm = /p/i.test(meridiem);
    if (isPm && hour < 12) hour += 12;
    if (!isPm && hour === 12) hour = 0;
  }

  let day = Number(first);
  let month = Number(secondNum);
  const monthFirstLocale = /^en-US/i.test(navigator.language || '');
  if (day > 12) {
    // unambiguous: day-first
  } else if (month > 12 || monthFirstLocale) {
    [day, month] = [month, day];
  }

  let fullYear = Number(year);
  if (fullYear < 100) fullYear += 2000;

  const date = new Date(
    fullYear,
    month - 1,
    day,
    hour,
    Number(minute),
    Number(second || 0)
  );
  if (!Number.isNaN(date.getTime())) {
    result.timestamp = Math.floor(date.getTime() / 1000);
  }
  return result;
}

/**
 * Get the bytes of an image that WhatsApp has already rendered.
 *
 * Two routes, because the obvious one does not work in Firefox: a `blob:` URL
 * belongs to the page's principal, and a content script has a different one, so
 * fetching it throws. Drawing the already-decoded <img> onto a canvas needs no
 * URL access at all -- the pixels are right there. The blob fetch is tried
 * first anyway since it preserves the original encoding exactly.
 */
async function imageToBase64(img) {
  let fetchError = '';
  try {
    return await blobToBase64(img.src);
  } catch (err) {
    fetchError = String(err && err.message ? err.message : err);
  }

  try {
    const width = img.naturalWidth || img.width;
    const height = img.naturalHeight || img.height;
    if (!width || !height) throw new Error('image not decoded yet');

    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    canvas.getContext('2d').drawImage(img, 0, 0);
    const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
    return { base64: dataUrl.split(',')[1], mimetype: 'image/jpeg', viaCanvas: true };
  } catch (err) {
    const canvasError = String(err && err.message ? err.message : err);
    throw new Error(`blob fetch: ${fetchError}; canvas: ${canvasError}`);
  }
}

async function blobToBase64(url) {
  const response = await fetch(url);
  const blob = await response.blob();
  const buffer = await blob.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return { base64: btoa(binary), mimetype: blob.type || 'image/jpeg' };
}

function extensionFor(mimetype) {
  const map = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
    'image/gif': '.gif',
    'audio/ogg': '.ogg',
    'audio/mpeg': '.mp3',
    'audio/mp4': '.m4a',
    'audio/aac': '.aac',
    'audio/wav': '.wav',
    'audio/webm': '.webm',
  };
  return map[(mimetype || '').split(';')[0]] || '.bin';
}

function chatIdFromDataId(dataId) {
  // "false_33612345678@c.us_3EB0..." / "true_..._..." for your own messages
  const parts = String(dataId).split('_');
  return parts.length >= 2 ? parts[1] : '';
}

/**
 * Media rows carry no data-pre-plain-text -- only a bare "19:08" in the bubble.
 * So we carry the date forward from the last row that did have full metadata
 * and combine it with that visible time. Flagged as inferred, never silently
 * presented as if it were read directly.
 */
const DOCUMENT_PATTERN =
  /([\w \-.()\[\]]+\.(?:docx?|xlsx?|pptx?|pdf|zip|rar|7z|csv|txt|log|json|xml|apk|mp4|mov|mp3|ogg))\b/i;

/**
 * Filename of a document attachment, if this row is one.
 *
 * Matched on the visible filename rather than WhatsApp's markup, because their
 * class names are hashed and change while a filename ending in .docx does not.
 */
function findDocumentName(row) {
  const title = row.querySelector('[title]');
  if (title) {
    const match = String(title.getAttribute('title') || '').match(DOCUMENT_PATTERN);
    if (match) return match[1].trim();
  }
  const match = String(row.innerText || '').match(DOCUMENT_PATTERN);
  return match ? match[1].trim() : '';
}

function findVisibleTime(row) {
  for (const node of row.querySelectorAll('span, div')) {
    const text = (node.textContent || '').trim();
    if (/^\d{1,2}:\d{2}(\s?[ap]\.?m\.?)?$/i.test(text)) return text;
  }
  return '';
}

function combineDateAndTime(dateParts, timeText) {
  const match = timeText.match(/^(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?$/i);
  if (!match || !dateParts) return null;
  let hour = Number(match[1]);
  if (match[3]) {
    const isPm = /p/i.test(match[3]);
    if (isPm && hour < 12) hour += 12;
    if (!isPm && hour === 12) hour = 0;
  }
  const date = new Date(
    dateParts.year,
    dateParts.month,
    dateParts.day,
    hour,
    Number(match[2])
  );
  return Number.isNaN(date.getTime()) ? null : Math.floor(date.getTime() / 1000);
}

/**
 * Every distinct duration ever observed for a voice-note row, keyed by its
 * data-id -- accumulated, never overwritten.
 *
 * A row's *first* reading is kept even if a later one turns out to be a
 * better match: an early, only-partially-loaded render of the slider can
 * report a duration slightly off from what the fully decoded audio turns out
 * to be. Keeping every distinct value seen means the matching step gets more
 * than one chance to pair the row correctly.
 */
function recordVoiceNoteDuration(voiceNoteDurations, row, dataId) {
  if (!row.querySelector(SELECTORS.voiceNoteLabel)) return;
  const durationSec = voiceNoteDurationSeconds(row);
  if (durationSec === null) return;
  if (!voiceNoteDurations.has(dataId)) voiceNoteDurations.set(dataId, new Set());
  voiceNoteDurations.get(dataId).add(durationSec);
}

/**
 * Pair each row's candidate durations with a captured clip, consuming clips
 * from `pool` as they're claimed so two rows never grab the same one.
 *
 * Exact matches are tried, across every row, before any row falls back to
 * the +/-1s tolerance -- so a row with an exact match never loses its clip to
 * a different row's fuzzy one just because it happened to be checked first.
 *
 * Shared between the "have we captured everything?" check in
 * ensureEveryVoiceNoteDurationIsCaptured and the real match in
 * buildMessages, so the two can never disagree about what counts as done.
 */
function matchVoiceNoteDurations(voiceNoteDurations, capturedPool) {
  const pool = capturedPool.slice();
  const matchedByRow = new Map();
  const unmatchedRowIds = [];

  const claim = (dataId, predicate) => {
    const index = pool.findIndex(predicate);
    if (index === -1) return false;
    const [entry] = pool.splice(index, 1);
    matchedByRow.set(dataId, entry);
    return true;
  };

  const rows = [...voiceNoteDurations.entries()];
  const stillNeeded = new Set(rows.map(([dataId]) => dataId));

  for (const [dataId, candidates] of rows) {
    for (const candidate of candidates) {
      if (claim(dataId, (entry) => entry.durationSec === candidate)) {
        stillNeeded.delete(dataId);
        break;
      }
    }
  }
  for (const dataId of stillNeeded) {
    const candidates = voiceNoteDurations.get(dataId);
    let matched = false;
    for (const candidate of candidates) {
      if (claim(dataId, (entry) => Math.abs(entry.durationSec - candidate) === 1)) {
        matched = true;
        break;
      }
    }
    if (!matched) unmatchedRowIds.push(dataId);
  }

  return { matchedByRow, unmatchedRowIds };
}

/**
 * Extract one row's full data and store it in `records`, unless already
 * present -- so a row already captured is never re-processed (in particular,
 * never re-fetched as an image) just because it is scanned again.
 *
 * Deliberately does *not* resolve a voice note's audio here: that only
 * exists as a pairing across every row and every captured clip
 * (matchVoiceNoteDurations), done once at the very end in buildMessages.
 * What this *does* record is a row's own voice-note duration reading(s),
 * via recordVoiceNoteDuration, alongside everything else -- one DOM pass per
 * row instead of two.
 *
 * `dateState` is the "carry the date forward for media rows with no
 * metadata" state from the original single-pass design, now threaded through
 * as an explicit object so it can persist correctly across many scan calls
 * over the course of a whole export, rather than being reset each time. Rows
 * are always scanned in their *current* DOM order (top to bottom is always
 * chronological for whatever is mounted at that moment, even though the
 * mounted window shifts over time), which is what makes carrying it forward
 * this way still valid.
 */
async function captureRow(row, records, voiceNoteDurations, dateState, stats) {
  const dataId = row.getAttribute('data-id');
  if (!dataId) return;

  // A voice note's own media is resolved later, in buildMessages -- matched
  // against page-hook.js's captured audio across every row at once, not
  // decided per row here. So at this point a voice note with no caption text
  // has no text, no media and no mediaError either, same as an empty system
  // row or a divider -- isVoiceNote is what tells the two apart below.
  const isVoiceNote = !!row.querySelector(SELECTORS.voiceNoteLabel);
  recordVoiceNoteDuration(voiceNoteDurations, row, dataId);

  if (records.has(dataId)) {
    // Already captured -- just keep the carry-forward date state moving if
    // this row had a real (not inferred) timestamp, so a not-yet-captured
    // row further up the conversation still infers from an accurate base.
    const existing = records.get(dataId);
    if (existing.timestamp && !existing.timestampInferred) {
      const date = new Date(existing.timestamp * 1000);
      dateState.lastDateParts = {
        year: date.getFullYear(),
        month: date.getMonth(),
        day: date.getDate(),
      };
      if (existing.senderRaw) dateState.lastSender = existing.senderRaw;
    }
    return;
  }

  const fromMe = dataId.startsWith('true_');
  const metaNode = row.querySelector(SELECTORS.meta);
  const meta = parseMeta(metaNode ? metaNode.dataset.prePlainText : '');

  let timestamp = meta.timestamp;
  let sender = meta.sender;
  let timestampInferred = false;

  if (timestamp) {
    const date = new Date(timestamp * 1000);
    dateState.lastDateParts = {
      year: date.getFullYear(),
      month: date.getMonth(),
      day: date.getDate(),
    };
    if (sender) dateState.lastSender = sender;
  } else {
    // Media rows: no metadata attribute, just a visible time in the bubble.
    const visible = findVisibleTime(row);
    const combined = combineDateAndTime(dateState.lastDateParts, visible);
    if (combined) {
      timestamp = combined;
      timestampInferred = true;
      stats.inferredStamps += 1;
    }
    sender = fromMe ? 'me' : dateState.lastSender || 'unknown';
  }

  const textNode = row.querySelector(SELECTORS.text);
  const text = textNode ? textNode.innerText : '';

  let media = null;
  let mediaError = '';
  const image = row.querySelector(SELECTORS.image);
  if (image && image.src) {
    try {
      const { base64, mimetype, viaCanvas } = await imageToBase64(image);
      media = { base64, mimetype, extension: extensionFor(mimetype) };
      if (viaCanvas) stats.viaCanvasCount += 1;
    } catch (err) {
      stats.imagesFailed += 1;
      const detail = String(err && err.message ? err.message : err);
      if (!stats.firstImageError) stats.firstImageError = detail;
      mediaError = `could not read the rendered image: ${detail}`;
    }
  }

  // Documents (.docx, .pdf, …) are not in the DOM as data -- WhatsApp only
  // decrypts them when you click download. So we record that one was sent,
  // with its filename, and the page says so. Better than pretending the
  // message was empty.
  let documentName = '';
  if (!media) {
    documentName = findDocumentName(row);
    if (documentName) {
      stats.documentsFound += 1;
      if (!mediaError) {
        mediaError = `document "${documentName}" was attached; download it from WhatsApp if you need it`;
      }
    }
  }

  if (!text && !media && !mediaError && !isVoiceNote) return; // system rows, dividers -- never recorded

  records.set(dataId, {
    dataId,
    fromMe,
    timestamp: timestamp || 0,
    timestampRaw: meta.raw,
    timestampInferred,
    senderRaw: sender,
    text,
    media,
    mediaError,
    documentName,
  });
}

/**
 * Scan every row currently mounted and capture whatever is new.
 *
 * This is the one place that walks the live DOM; every scroll checkpoint
 * calls it, so a row only ever gets its (possibly slow, image-fetching)
 * capture done once, the first time it is seen, however many more times it
 * is scanned afterward.
 */
async function captureEverything(records, voiceNoteDurations, dateState, stats) {
  for (const row of document.querySelectorAll(SELECTORS.row)) {
    await captureRow(row, records, voiceNoteDurations, dateState, stats);
  }
}

const CHECKPOINT_COUNT = 4;
const CHECKPOINT_SETTLE_MS = 350;

/**
 * Move a scroll container to the top via a few evenly-spaced stops rather
 * than one jump.
 *
 * `container.scrollTop = 0` in a single assignment skips straight there --
 * WhatsApp's virtualized list only fully mounts a row's rich internal widgets
 * (a voice note's slider included) for whatever ends up near the viewport
 * right after that jump lands. Any row that was only ever *between* two
 * jump positions, never the settled one, may never get mounted at all,
 * which is why voice-note capture was landing inconsistently between runs.
 *
 * A first attempt stepped pixel-by-pixel all the way to the top, which is
 * unbounded: on a long conversation, or if WhatsApp keeps prepending more
 * history the closer it gets to the top (extending the distance left to
 * cover faster than fixed-size steps consume it), that loop can run for a
 * very long time. A fixed number of checkpoints between wherever we started
 * and the top costs a bounded amount of extra time per pass regardless of
 * how long the conversation is, while still giving intermediate rows more
 * than one instant to mount.
 */
async function scrollToTopGradually(container, onStep) {
  const start = container.scrollTop;
  for (let i = 1; i <= CHECKPOINT_COUNT; i += 1) {
    container.scrollTop = Math.max(0, Math.round(start - (start * i) / CHECKPOINT_COUNT));
    await sleep(CHECKPOINT_SETTLE_MS);
    await onStep();
  }
  container.scrollTop = 0;
}

/**
 * Scroll upwards to pull history out of the phone and into the DOM,
 * capturing every row's full data along the way.
 *
 * `passes` (the "scrollbacks" setting) is a deliberate, user-set bound on how
 * far back to go -- exporting is meant to cover recent activity, not a
 * chat's entire lifetime (the Python side already dedupes re-exports by
 * content fingerprint, precisely so this only has to cover what is new since
 * last time). So this never tries to scroll past that bound looking for "the
 * true top": reaching it, or running out of passes first, are both a normal
 * end to this phase, not a problem to fix by scrolling further.
 *
 * Stops early if the *cumulative number of distinct rows ever captured*
 * (records.size) stops growing for two consecutive passes -- not the live
 * `[data-id]` count in the DOM right now. WhatsApp caps how many rows stay
 * mounted at once (observed at exactly 100): past that cap, the live count
 * plateaus permanently as old rows are evicted to make room for new ones,
 * even while genuinely new history keeps loading within the requested
 * `passes` budget. records.size only ever grows, so it is what actually
 * reflects whether there is more to find within that budget.
 */
async function loadHistory(passes, report, records, voiceNoteDurations, dateState, stats) {
  const container = findScrollContainer();
  if (!container) return;

  const capture = () => captureEverything(records, voiceNoteDurations, dateState, stats);
  await capture();
  let previous = records.size;
  // A single pass with no growth is not reliable proof there is nothing more
  // to load. Exporting more than once in the same page load without a
  // reload is a real workflow, and by then WhatsApp may already have this
  // chat's full history sitting in its own client-side cache -- re-mounting
  // it here can still take more than one scroll-and-settle cycle to catch
  // up, so one flat reading can be a momentary plateau rather than the true
  // top. Two flat readings in a row is what actually means "done".
  let stableStreak = 0;
  for (let pass = 1; pass <= passes; pass += 1) {
    await scrollToTopGradually(container, capture);
    await sleep(SCROLL_SETTLE_MS);
    await capture();
    const current = records.size;
    report(`scrollback ${pass}/${passes} — ${current} messages loaded`);
    if (current === previous) {
      stableStreak += 1;
      if (stableStreak >= 2) {
        report(`reached the top after ${pass} pass(es) — ${current} messages`);
        break;
      }
    } else {
      stableStreak = 0;
    }
    previous = current;
  }

  await ensureEveryVoiceNoteDurationIsCaptured(container, records, voiceNoteDurations, dateState, stats, report);
}

const VOICE_NOTE_RETRY_ATTEMPTS = 10;
const WIGGLE_SETTLE_MS = 700;

/**
 * Revisit a random position *within the range already loaded* -- never all
 * the way to the top -- to give a row whose slider didn't mount the first
 * time another chance.
 *
 * This deliberately avoids scrollTop = 0: hitting exactly that position is
 * what asks WhatsApp for more history (that's the whole mechanism
 * loadHistory's passes rely on), and this phase must not do that -- it needs
 * to stay within the same window loadHistory already bounded, not keep
 * pulling in more of the conversation on every retry (which is what an
 * earlier version of this code did, chasing a target that kept growing for
 * as long as scrollTop kept touching 0).
 */
async function wiggleAndCapture(container, capture) {
  const range = Math.max(1, container.scrollHeight - container.clientHeight);
  // Only the very top risks asking WhatsApp for more history -- the bottom
  // (most recent messages) carries no such risk, so only that one edge needs
  // a margin. A small fixed distance rather than a percentage of the total
  // height: on a long conversation, 5% of everything loaded so far excluded
  // a sizeable chunk of the *most recently loaded* rows -- exactly the ones
  // most likely to still need a chance to mount, since they only just
  // arrived in the last pass.
  const topMargin = Math.min(range, 40);
  const target = topMargin + Math.floor(Math.random() * Math.max(1, range - topMargin));
  container.scrollTop = target;
  await sleep(WIGGLE_SETTLE_MS);
  await capture();
}

/**
 * Give every voice note a further, bounded number of chances to have its
 * slider mount, for whichever ones the main scrollback pass missed.
 *
 * A row's slider is only mounted while its row is near the viewport, and a
 * single pass through the conversation does not guarantee every row spent
 * enough time there -- this is what actually catches the stragglers. It is
 * a small fixed number of extra attempts, not an open-ended search: it must
 * stay within the window loadHistory already established (see
 * wiggleAndCapture) rather than scroll further back looking for more, and it
 * stops as soon as everything currently known about is matched.
 */
async function ensureEveryVoiceNoteDurationIsCaptured(container, records, voiceNoteDurations, dateState, stats, report) {
  const capture = () => captureEverything(records, voiceNoteDurations, dateState, stats);
  for (let attempt = 1; attempt <= VOICE_NOTE_RETRY_ATTEMPTS; attempt += 1) {
    const pool = readCapturedAudioLog();
    if (pool.length === 0) return;
    const { unmatchedRowIds } = matchVoiceNoteDurations(voiceNoteDurations, pool);
    if (unmatchedRowIds.length === 0) return;

    report(
      `voice notes: ${pool.length - unmatchedRowIds.length}/${pool.length} matched -- extra scan ${attempt}`
    );
    await wiggleAndCapture(container, capture);
  }

  const pool = readCapturedAudioLog();
  const { unmatchedRowIds } = matchVoiceNoteDurations(voiceNoteDurations, pool);
  if (unmatchedRowIds.length > 0) {
    report(
      `gave up trying to match every voice note -- ` +
      `${unmatchedRowIds.length} of ${pool.length} still unmatched; reload and export again`
    );
  }
}

/**
 * Turn accumulated per-row records into the final message list.
 *
 * Records are stored in *discovery* order, not chronological order (rows
 * near the top get discovered later, scrolling backwards through history),
 * so the result is sorted by timestamp before it is returned.
 */
function buildMessages({ includeOwn, records, voiceNoteDurations, stats, report }) {
  const { matchedByRow, unmatchedRowIds } = matchVoiceNoteDurations(
    voiceNoteDurations,
    readCapturedAudioLog()
  );
  if (unmatchedRowIds.length) {
    console.log('[ticket-parser] voice notes still unmatched at export time:', unmatchedRowIds);
  }

  const messages = [];
  let skippedOwn = 0;
  let voiceNotesFound = 0;
  let voiceNotesUnmatched = 0;

  for (const rec of records.values()) {
    if (rec.fromMe && !includeOwn) {
      skippedOwn += 1;
      continue;
    }

    let media = rec.media;
    let mediaError = rec.mediaError;
    let mediaKind = media ? 'image' : '';

    if (!media && voiceNoteDurations.has(rec.dataId)) {
      voiceNotesFound += 1;
      const entry = matchedByRow.get(rec.dataId);
      if (!entry) {
        voiceNotesUnmatched += 1;
        mediaError =
          `voice note (${[...voiceNoteDurations.get(rec.dataId)].join('/')}s) had a duration but no ` +
          'captured audio matched it, even after retrying for up to a minute; reload the page ' +
          'and export again';
      } else {
        media = { base64: entry.base64, mimetype: entry.mimetype, extension: extensionFor(entry.mimetype) };
        mediaKind = 'audio';
      }
    }

    messages.push({
      schema: 1,
      id: rec.dataId,
      chat_id: chatIdFromDataId(rec.dataId),
      author_id: chatIdFromDataId(rec.dataId),
      author_name: rec.senderRaw || (rec.fromMe ? 'me' : 'unknown'),
      timestamp: rec.timestamp,
      timestamp_raw: rec.timestampRaw,
      timestamp_inferred: rec.timestampInferred,
      type: mediaKind || (rec.documentName ? 'document' : 'chat'),
      document_name: rec.documentName,
      text: rec.text,
      from_me: rec.fromMe,
      media,
      media_error: mediaError,
      source: 'firefox-extension',
    });
  }

  messages.sort((a, b) => a.timestamp - b.timestamp);

  report(
    `collected ${messages.length} messages` +
      (skippedOwn ? `, skipped ${skippedOwn} of your own` : '') +
      (stats.inferredStamps ? `, ${stats.inferredStamps} time(s) inferred from the bubble` : '') +
      (stats.viaCanvasCount ? `, ${stats.viaCanvasCount} image(s) read via canvas` : '') +
      (stats.documentsFound ? `, ${stats.documentsFound} document(s) noted` : '') +
      (voiceNotesFound ? `, ${voiceNotesFound} voice note(s) found` : '') +
      (stats.imagesFailed ? `, ${stats.imagesFailed} image(s) unreadable` : '') +
      (voiceNotesUnmatched ? `, ${voiceNotesUnmatched} voice note(s) unmatched` : '')
  );
  if (stats.firstImageError) report(`first image error: ${stats.firstImageError}`);
  return messages;
}

browser.runtime.onMessage.addListener(async (request) => {
  if (request.command !== 'scrape') return undefined;

  const report = (text) =>
    browser.runtime.sendMessage({ command: 'progress', text }).catch(() => {});

  if (!document.querySelector(SELECTORS.main)) {
    return { ok: false, error: 'No conversation is open. Click a chat first.' };
  }

  try {
    // page-hook.js's captured-audio list is deliberately never reset here.
    // WhatsApp appears to call createObjectURL for a given voice note only
    // once per page load -- reusing the cached blob on later scrolls past
    // the same row rather than recreating it -- so wiping that list at the
    // start of each export would permanently lose any clip only ever seen
    // once, early in the session. matchVoiceNoteDurations recomputes fresh
    // per export from the *current* voiceNoteDurations map below, so a
    // leftover clip from an earlier export (or even a different
    // conversation) can only ever be wrongly claimed by an actual duration
    // collision -- a much smaller risk than losing captures outright.
    const records = new Map(); // data-id -> full message record, caught during the scroll
    const voiceNoteDurations = new Map(); // data-id -> Set<seconds>, caught during the scroll
    const dateState = { lastDateParts: null, lastSender: '' };
    const stats = {
      inferredStamps: 0,
      viaCanvasCount: 0,
      imagesFailed: 0,
      documentsFound: 0,
      firstImageError: '',
    };

    await loadHistory(Number(request.scrollbacks) || 0, report, records, voiceNoteDurations, dateState, stats);
    // One last catch for anything still visible right now that loadHistory's
    // per-pass scans didn't already record.
    await captureEverything(records, voiceNoteDurations, dateState, stats);
    if (voiceNoteDurations.size) {
      await sleep(AUDIO_SETTLE_MS);
    }

    const messages = buildMessages({
      includeOwn: Boolean(request.includeOwn),
      records,
      voiceNoteDurations,
      stats,
      report,
    });

    if (!messages.length) {
      return {
        ok: false,
        error:
          'Found no messages. WhatsApp may have changed its markup — check SELECTORS in content.js.',
      };
    }
    return { ok: true, messages };
  } catch (err) {
    return { ok: false, error: String(err && err.message ? err.message : err) };
  }
});
