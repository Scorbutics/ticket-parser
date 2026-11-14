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
  // "Select messages" mode: each row gets a checkbox. It may sit beside the
  // [data-id] element rather than inside it, so it is looked up within the
  // enclosing list row (role="row") -- see isRowSelected.
  selectionScope: '[role="row"]',
  selectionCheckbox: '[role="checkbox"], input[type="checkbox"]',
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

/** Same idea for documents: see the document half of page-hook.js. */
function readCapturedDocumentLog() {
  try {
    const raw = window.wrappedJSObject && window.wrappedJSObject.__ticketParserDocumentLogJSON;
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
 * "[09:12, 15/01/2024] Emmanuel DE: " -> { dateParts, clock, sender, raw }
 *
 * The date is returned *unresolved*: "02/11/2026" is 2 November or 11
 * February depending on the account's display format, and nothing in a single
 * stamp says which. The browser locale does not either -- an en-US Firefox can
 * perfectly well show a French WhatsApp. So the order is decided once for the
 * whole export, from stamps that settle it (a 13+ in either position), by
 * resolveDateOrder(); see buildMessages.
 */
function parseMeta(raw) {
  const result = { dateParts: null, clock: null, sender: '', raw: raw || '' };
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

  let fullYear = Number(year);
  if (fullYear < 100) fullYear += 2000;

  result.dateParts = { first: Number(first), second: Number(secondNum), year: fullYear };
  result.clock = { hour, minute: Number(minute), second: Number(second || 0) };
  return result;
}

/** Note what an unresolved date says about the export's day/month order. */
function recordDateEvidence(dateState, dateParts) {
  if (dateParts.first > 12) dateState.dayFirstEvidence += 1;
  if (dateParts.second > 12) dateState.monthFirstEvidence += 1;
}

/**
 * 'day-first' or 'month-first' for the whole export.
 *
 * One conversation is rendered in one format, so a single unambiguous stamp
 * settles every ambiguous one. Only when the export contains no stamp with a
 * 13+ at all (a short export early in a month) does it fall back to the
 * browser locale -- and says so.
 */
function resolveDateOrder(dateState) {
  const { dayFirstEvidence, monthFirstEvidence } = dateState;
  if (dayFirstEvidence && !monthFirstEvidence) return { order: 'day-first', guessed: false };
  if (monthFirstEvidence && !dayFirstEvidence) return { order: 'month-first', guessed: false };
  if (dayFirstEvidence && monthFirstEvidence) {
    // Contradictory; go with the majority and flag it.
    return {
      order: dayFirstEvidence >= monthFirstEvidence ? 'day-first' : 'month-first',
      guessed: true,
    };
  }
  const monthFirstLocale = /^en-US/i.test(navigator.language || '');
  return { order: monthFirstLocale ? 'month-first' : 'day-first', guessed: true };
}

/** Seconds since the epoch for unresolved parts, or 0 if there is no date. */
function toTimestamp(dateParts, clock, order) {
  if (!dateParts || !clock) return 0;
  const { first, second, year } = dateParts;
  let day = order === 'month-first' ? second : first;
  let month = order === 'month-first' ? first : second;
  // A single stamp that cannot be read the chosen way is still unambiguous.
  if (month > 12 && day <= 12) [day, month] = [month, day];
  const date = new Date(year, month - 1, day, clock.hour, clock.minute, clock.second);
  return Number.isNaN(date.getTime()) ? 0 : Math.floor(date.getTime() / 1000);
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

const DOCUMENT_EXTENSIONS = 'docx?|xlsx?|pptx?|pdf|odt|ods|odp|rtf|zip|rar|7z|csv|txt|md|log|json|xml|apk|mp4|mov|mp3|ogg';
const DOCUMENT_NAME = new RegExp(`\\.(?:${DOCUMENT_EXTENSIONS})$`, 'i');
// The line under a document's name: "3 pages • DOCX • 689 kB", "PDF • 1,2 Mo".
const DOCUMENT_META = /(?:^|[•·]\s*)([A-Za-z0-9]{2,5})\s*[•·]\s*(\d+(?:[.,]\d+)?)\s*([kKMG]?)[bBoO]\s*$/;

/**
 * A document attachment's name, type and approximate size, if this row is one.
 *
 * Read from the visible text rather than WhatsApp's markup, because their
 * class names are hashed and change while "• DOCX • 689 kB" does not. A row
 * only counts as a document if it has that size line or a title attribute
 * naming a file -- a text message that merely *mentions* "rapport.pdf" does
 * not.
 *
 * The visible name is often truncated ("V2-Amélioration PEPS – Ma Base…");
 * the full one, when WhatsApp provides it, is in a title attribute.
 */
function findDocumentInfo(row) {
  let fullName = '';
  for (const node of row.querySelectorAll('[title]')) {
    const title = String(node.getAttribute('title') || '').trim();
    if (DOCUMENT_NAME.test(title)) {
      fullName = title;
      break;
    }
  }

  const lines = String(row.innerText || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  const metaIndex = lines.findIndex((line) => DOCUMENT_META.test(line));
  if (!fullName && metaIndex === -1) return null;

  let type = '';
  let size = null;
  if (metaIndex !== -1) {
    const [, kind, amount, unit] = lines[metaIndex].match(DOCUMENT_META);
    type = kind.toLowerCase();
    size = { amount: Number(amount.replace(',', '.')), power: ' kMG'.indexOf(unit.toUpperCase() === 'K' ? 'k' : unit.toUpperCase() || ' '), decimals: /[.,]/.test(amount) };
    if (size.power < 0) size.power = 0;
  }

  let name = fullName || lines.find((line) => DOCUMENT_NAME.test(line)) || '';
  let truncated = false;
  if (!name && metaIndex > 0) {
    name = lines[metaIndex - 1];
    truncated = /…|\.\.\.$/.test(name);
    name = name.replace(/(…|\.\.\.)$/, '').trim();
  }
  if (!type) {
    const ext = name.match(/\.([A-Za-z0-9]+)$/);
    type = ext ? ext[1].toLowerCase() : '';
  }
  return { name, truncated, type, size };
}

const normalizeName = (name) => String(name || '').normalize('NFC').toLowerCase().replace(/\s+/g, ' ').trim();

/**
 * The byte range a displayed "689 kB" can stand for (either base, rounding).
 * Written into the export so `ticket-parser` can find the file on disk later.
 */
function sizeRange(size) {
  if (!size) return null;
  let min = Infinity;
  let max = 0;
  for (const base of [1000, 1024]) {
    const tolerance = (size.decimals ? 0.05 : 0.5) + size.amount * 0.01;
    min = Math.min(min, (size.amount - tolerance) * base ** size.power);
    max = Math.max(max, (size.amount + tolerance) * base ** size.power);
  }
  return { min: Math.max(0, Math.floor(min)), max: Math.ceil(max) };
}

/** Whether a captured file's byte length fits the "689 kB" WhatsApp displayed. */
function sizeMatches(size, byteLength) {
  if (!size) return false;
  return [1000, 1024].some((base) => {
    const value = byteLength / base ** size.power;
    const tolerance = (size.decimals ? 0.05 : 0.5) + size.amount * 0.01;
    return Math.abs(value - size.amount) <= tolerance;
  });
}

/**
 * Pair document rows with the files page-hook.js caught being downloaded.
 *
 * By name first (full, or the visible prefix of a truncated one); otherwise
 * by type and displayed size. Each captured file is claimed at most once.
 */
function matchDocuments(records, capturedPool) {
  const pool = capturedPool.slice();
  const matched = new Map();
  const docs = [...records.values()].filter((rec) => rec.document);
  const claim = (rec, predicate) => {
    const index = pool.findIndex(predicate);
    if (index === -1) return false;
    matched.set(rec.dataId, pool.splice(index, 1)[0]);
    return true;
  };

  const rest = docs.filter((rec) => {
    const wanted = normalizeName(rec.document.name);
    if (!wanted) return true;
    return !claim(rec, (entry) => {
      const got = normalizeName(entry.name);
      return got && (rec.document.truncated ? got.startsWith(wanted) : got === wanted);
    });
  });
  for (const rec of rest) {
    claim(rec, (entry) => {
      const ext = (String(entry.name).match(/\.([A-Za-z0-9]+)$/) || [])[1] || '';
      const typeOk = !entry.name || !rec.document.type || ext.toLowerCase() === rec.document.type;
      return typeOk && sizeMatches(rec.document.size, entry.byteLength);
    });
  }
  return matched;
}

/**
 * The bubble's own "19:08", which WhatsApp renders last, bottom-right.
 *
 * The *last* match, not the first: a voice note or video shows its duration
 * ("0:42", "1:26") earlier in the bubble, and that reads exactly like a time
 * of day. Anything inside the voice-note slider is skipped for the same reason.
 */
function findVisibleTime(row) {
  let found = '';
  for (const node of row.querySelectorAll('span, div')) {
    if (node.closest(SELECTORS.voiceSlider)) continue;
    const text = (node.textContent || '').trim();
    if (/^\d{1,2}:\d{2}(\s?[ap]\.?m\.?)?$/i.test(text)) found = text;
  }
  return found;
}

function parseVisibleClock(timeText) {
  const match = timeText.match(/^(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?$/i);
  if (!match) return null;
  let hour = Number(match[1]);
  if (match[3]) {
    const isPm = /p/i.test(match[3]);
    if (isPm && hour < 12) hour += 12;
    if (!isPm && hour === 12) hour = 0;
  }
  return { hour, minute: Number(match[2]), second: 0 };
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
 * Whether a row is ticked in WhatsApp's "Select messages" mode.
 *
 * Only reads the checkbox state; never clicks it.
 */
function isRowSelected(row) {
  const scope = row.closest(SELECTORS.selectionScope) || row.parentElement || row;
  if (scope.getAttribute('aria-selected') === 'true') return true;
  for (const box of scope.querySelectorAll(SELECTORS.selectionCheckbox)) {
    if (box.getAttribute('aria-checked') === 'true' || box.checked === true) return true;
  }
  return false;
}

function selectionModeActive() {
  const main = document.querySelector(SELECTORS.main);
  return !!(main && main.querySelector(SELECTORS.selectionCheckbox));
}

const clockMinutes = (clock) => clock.hour * 60 + clock.minute;
const sameDay = (a, b) => a.first === b.first && a.second === b.second && a.year === b.year;

/**
 * The date of a row that only shows a bare "19:08", from the dated rows on
 * either side of it.
 *
 * `prev`/`next` are the nearest rows *in the same scan* that carried a real
 * data-pre-plain-text. Within one scan the mounted rows are contiguous, so
 * these really are its chronological neighbours -- which is what the old
 * "carry the last date forward" approach got wrong: scrolling upward prepends
 * older rows at the top, so they inherited the date of whatever row the
 * *previous* scan ended on (usually today), or nothing at all.
 *
 * Returns { dateParts, confident } or null when nothing safe can be said.
 */
function inferDate(clock, prev, next) {
  const minutes = clockMinutes(clock);
  if (prev && next) {
    if (sameDay(prev.dateParts, next.dateParts)) return { dateParts: prev.dateParts, confident: true };
    // The day changed somewhere between the two: before midnight it belongs
    // to the earlier one, after it to the later one.
    const dateParts = minutes >= clockMinutes(prev.clock) ? prev.dateParts : next.dateParts;
    return { dateParts, confident: true };
  }
  if (prev && minutes >= clockMinutes(prev.clock)) return { dateParts: prev.dateParts, confident: false };
  if (next && minutes <= clockMinutes(next.clock)) return { dateParts: next.dateParts, confident: false };
  return null;
}

/**
 * Fill in the date (and, for the other party, the sender) of rows that had
 * no metadata, using their neighbours in this scan's DOM order.
 *
 * Re-run on every scan until a row has neighbours on both sides: a row at the
 * very edge of the mounted window only gets a provisional answer, which a
 * later scan -- with more history mounted above it -- can firm up.
 */
function inferMissingDates(order, session) {
  const { records, anchors, stats } = session;
  const nearest = (from, step, predicate) => {
    for (let i = from + step; i >= 0 && i < order.length; i += step) {
      const anchor = anchors.get(order[i]);
      if (anchor && predicate(anchor)) return anchor;
    }
    return null;
  };

  order.forEach((dataId, index) => {
    const rec = records.get(dataId);
    if (!rec || !rec.needsDate) return;

    if (!rec.fromMe && !rec.senderRaw) {
      const other = (a) => !a.fromMe && a.sender;
      const neighbour = nearest(index, -1, other) || nearest(index, 1, other);
      if (neighbour) rec.senderRaw = neighbour.sender;
    }

    if (!rec.clock || rec.dateConfident) return;
    const any = () => true;
    const inferred = inferDate(rec.clock, nearest(index, -1, any), nearest(index, 1, any));
    if (!inferred) return;
    if (!rec.timestampInferred) stats.inferredStamps += 1;
    rec.dateParts = inferred.dateParts;
    rec.timestampInferred = true;
    rec.dateConfident = inferred.confident;
  });
}

/**
 * Extract one row's data.
 *
 * Every row with metadata becomes an *anchor* (date, clock, sender) -- even
 * one outside the range -- because anchors are what undated rows borrow their
 * date from. Only rows with `full` set (possibly inside the range) become full
 * *records* (text, image, voice-note duration), and each only once: a row
 * already captured is never re-processed (in particular, never re-fetched as
 * an image) just because it is scanned again.
 *
 * Deliberately does *not* resolve a voice note's audio here: that only exists
 * as a pairing across every row and every captured clip
 * (matchVoiceNoteDurations), done once at the very end in buildMessages.
 *
 */
async function captureRow(row, session, full) {
  const { records, anchors, voiceNoteDurations, dateState, stats } = session;
  const dataId = row.getAttribute('data-id');
  if (!dataId) return null;

  const fromMe = dataId.startsWith('true_');
  const metaNode = row.querySelector(SELECTORS.meta);
  const meta = parseMeta(metaNode ? metaNode.dataset.prePlainText : '');

  if (meta.dateParts && !anchors.has(dataId)) {
    anchors.set(dataId, { dateParts: meta.dateParts, clock: meta.clock, sender: meta.sender, fromMe });
    recordDateEvidence(dateState, meta.dateParts);
  }

  if (!full) return;

  // A voice note's own media is resolved later, in buildMessages -- matched
  // against page-hook.js's captured audio across every row at once, not
  // decided per row here. So at this point a voice note with no caption text
  // has no text, no media and no mediaError either, same as an empty system
  // row or a divider -- isVoiceNote is what tells the two apart below.
  const isVoiceNote = !!row.querySelector(SELECTORS.voiceNoteLabel);
  recordVoiceNoteDuration(voiceNoteDurations, row, dataId);

  if (records.has(dataId)) return;

  // Rows without metadata (media, documents, voice notes) only show a bare
  // "19:08"; their date is filled in from their neighbours by
  // inferMissingDates, and flagged as inferred -- never silently presented as
  // if it were read directly.
  const needsDate = !meta.dateParts;
  const clock = needsDate ? parseVisibleClock(findVisibleTime(row)) : meta.clock;
  const sender = needsDate ? (fromMe ? 'me' : '') : meta.sender;

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
  // Documents (.docx, .pdf, …) are only decrypted when someone clicks their
  // download; their contents are paired up later from what page-hook.js
  // caught (matchDocuments). Here we just note that one was sent.
  const docInfo = media ? null : findDocumentInfo(row);
  if (docInfo) stats.documentsFound += 1;

  if (!text && !media && !mediaError && !isVoiceNote && !docInfo) return; // system rows, dividers

  records.set(dataId, {
    dataId,
    fromMe,
    // Resolved to a timestamp in buildMessages, once the whole export has
    // been seen and the day/month order is known.
    dateParts: meta.dateParts,
    clock,
    needsDate,
    dateConfident: false,
    timestampRaw: meta.raw,
    timestampInferred: false,
    senderRaw: sender,
    text,
    media,
    mediaError,
    document: docInfo,
  });
}

/**
 * Fold one scan's row order into the whole-export timeline.
 *
 * WhatsApp only keeps ~100 rows mounted, so no single scan sees the whole
 * range. But each scan is a contiguous run of the chat, in order, and
 * consecutive scans overlap (we scroll less than a screen at a time), so
 * stitching them on the rows they share gives the exact order of everything
 * seen -- which is what "everything between the first and last ticked
 * message" is cut from. Timestamps could not do this: inferred ones can be
 * off, and many messages share a minute.
 */
function mergeIntoTimeline(timeline, order) {
  if (!timeline.length) return order.slice();
  const known = new Set(timeline);
  const firstShared = order.findIndex((id) => known.has(id));
  if (firstShared === -1) {
    // No overlap at all: we scrolled up past everything previously seen.
    return order.concat(timeline);
  }
  const result = timeline.slice();
  // Rows above the first shared one are older: they go just before it.
  const head = order.slice(0, firstShared);
  result.splice(result.indexOf(order[firstShared]), 0, ...head);
  // Unknown rows below it (rare: something mounted mid-window) go right
  // after their predecessor in this scan.
  for (let i = firstShared + 1; i < order.length; i += 1) {
    if (known.has(order[i])) continue;
    result.splice(result.indexOf(order[i - 1]) + 1, 0, order[i]);
  }
  return result;
}

/** Timeline positions of the oldest and newest ticked rows seen so far. */
function rangeBounds(session) {
  let first = -1;
  let last = -1;
  session.timeline.forEach((id, index) => {
    if (!session.selectedIds.has(id)) return;
    if (first === -1) first = index;
    last = index;
  });
  return { first, last };
}

/**
 * Scan every row currently mounted and capture whatever may be in the range.
 *
 * Two passes: the first is cheap (ids, tick state, order); the second does
 * the real -- possibly slow, image-fetching -- capture, but only for rows at
 * or above the newest ticked message. Everything below it is outside the
 * range and is never exported, so its screenshots are not even read. Rows
 * above the oldest tick seen *so far* are captured, since a tick further up
 * may still turn up; the surplus is trimmed in buildMessages.
 *
 * Returns how many ticked rows are mounted, and how many rows sit above the
 * topmost of them.
 *
 * `extend` is false for revisits (the voice-note retries jump to random
 * positions): those only re-capture rows already in the timeline, and never
 * stitch a possibly non-overlapping scan into it.
 */
async function captureEverything(session, { extend = true } = {}) {
  const rows = [];
  const order = [];
  let selected = 0;
  let rowsAboveFirstSelected = -1;
  for (const row of document.querySelectorAll(SELECTORS.row)) {
    const dataId = row.getAttribute('data-id');
    if (!dataId) continue;
    if (extend && isRowSelected(row)) {
      session.selectedIds.add(dataId);
      selected += 1;
      if (rowsAboveFirstSelected === -1) rowsAboveFirstSelected = order.length;
    }
    order.push(dataId);
    rows.push(row);
  }
  if (extend) session.timeline = mergeIntoTimeline(session.timeline, order);

  const { last } = rangeBounds(session);
  const position = new Map(session.timeline.map((id, index) => [id, index]));
  for (let i = 0; i < rows.length; i += 1) {
    const full = last !== -1 && position.has(order[i]) && position.get(order[i]) <= last;
    await captureRow(rows[i], session, full);
  }
  inferMissingDates(order, session);
  return { selected, rowsAboveFirstSelected };
}

const RANGE_STEP_FRACTION = 0.8;
const RANGE_MAX_STEPS = 600;
// Once both ends are found, this many unticked rows mounted above the
// topmost tick confirm there is no further tick above it (in case more than
// two were left ticked: the range then runs from the oldest to the newest).
const RANGE_CONFIRM_ROWS = 40;

/**
 * Read everything between the first and the last ticked message.
 *
 * Starts at the bottom of the chat -- so the newest tick is always met on the
 * way, wherever you had scrolled to -- and moves up by a bit less than a
 * screen at a time, so every row passes through the viewport (a voice note's
 * slider only mounts there) and consecutive scans overlap (mergeIntoTimeline
 * relies on it). Stops once two ticks are found and nothing ticked is
 * mounted above the topmost one, or at the start of the chat.
 */
async function loadRange(report, session) {
  const container = findScrollContainer();
  if (!container) return;
  const { records, anchors } = session;

  container.scrollTop = container.scrollHeight;
  await sleep(SCROLL_SETTLE_MS);
  let scan = await captureEverything(session);

  const progress = () => {
    const { first, last } = rangeBounds(session);
    const ticks = session.selectedIds.size;
    if (ticks === 0) return 'looking for the last ticked message…';
    const inRange = last - first + 1;
    return ticks === 1
      ? `last message found — looking for the first (${inRange} message(s) so far)`
      : `first and last found — ${inRange} message(s) in range`;
  };
  report(progress());

  let flatAtTop = 0;
  for (let step = 1; step <= RANGE_MAX_STEPS; step += 1) {
    if (
      session.selectedIds.size >= 2 &&
      (scan.selected === 0 || scan.rowsAboveFirstSelected >= RANGE_CONFIRM_ROWS)
    ) {
      break;
    }
    const before = { top: container.scrollTop, known: anchors.size + records.size + session.timeline.length };
    container.scrollTop = Math.max(0, before.top - Math.round(container.clientHeight * RANGE_STEP_FRACTION));
    await sleep(SCROLL_SETTLE_MS);
    scan = await captureEverything(session);
    report(`step ${step} — ${progress()}`);

    // Sitting at the top with nothing new loading twice running: that is
    // the start of the chat.
    const atTop = before.top === 0 && container.scrollTop === 0;
    const known = anchors.size + records.size + session.timeline.length;
    flatAtTop = atTop && known === before.known ? flatAtTop + 1 : 0;
    if (flatAtTop >= 2) {
      report('reached the start of the conversation');
      break;
    }
  }

  await ensureEveryVoiceNoteDurationIsCaptured(container, session, report);
}

const VOICE_NOTE_RETRY_ATTEMPTS = 10;
const WIGGLE_SETTLE_MS = 700;

/**
 * Revisit a random position *within the range already loaded* -- never all
 * the way to the top -- to give a row whose slider didn't mount the first
 * time another chance.
 *
 * This deliberately avoids scrollTop = 0: hitting exactly that position is
 * what asks WhatsApp for more history, and this phase must not do that -- it
 * needs to stay within the window loadRange already covered, not keep
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
 * stay within the window loadRange already covered (see
 * wiggleAndCapture) rather than scroll further back looking for more, and it
 * stops as soon as everything currently known about is matched.
 */
async function ensureEveryVoiceNoteDurationIsCaptured(container, session, report) {
  const { voiceNoteDurations } = session;
  const capture = () => captureEverything(session, { extend: false });
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
function buildMessages(session, report) {
  const { includeOwn, voiceNoteDurations, dateState, stats } = session;
  const dateOrder = resolveDateOrder(dateState);

  // Only what lies between the first and last tick, inclusive.
  const { first, last } = rangeBounds(session);
  const inRange = new Set(session.timeline.slice(first, last + 1));
  const records = new Map([...session.records].filter(([dataId]) => inRange.has(dataId)));

  const { matchedByRow, unmatchedRowIds } = matchVoiceNoteDurations(
    voiceNoteDurations,
    readCapturedAudioLog()
  );
  if (unmatchedRowIds.length) {
    console.log('[ticket-parser] voice notes still unmatched at export time:', unmatchedRowIds);
  }

  const documentsByRow = matchDocuments(records, readCapturedDocumentLog());
  const documentsMissing = [];
  let imagesUnreadable = 0;

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

    let documentFile = null;
    const documentName = rec.document ? rec.document.name : '';
    if (rec.document) {
      const entry = documentsByRow.get(rec.dataId);
      if (entry) {
        documentFile = { base64: entry.base64, mimetype: entry.mimetype, filename: entry.name || documentName };
      } else {
        documentsMissing.push(documentName || 'unnamed document');
        mediaError =
          mediaError ||
          `document "${documentName}" was attached but not downloaded in WhatsApp before ` +
            'exporting; click its download in this tab and export again to include its contents';
      }
    }

    if (rec.media === null && /^could not read the rendered image/.test(rec.mediaError || '')) imagesUnreadable += 1;

    messages.push({
      schema: 1,
      id: rec.dataId,
      chat_id: chatIdFromDataId(rec.dataId),
      author_id: chatIdFromDataId(rec.dataId),
      author_name: rec.senderRaw || (rec.fromMe ? 'me' : 'unknown'),
      timestamp: toTimestamp(rec.dateParts, rec.clock, dateOrder.order),
      timestamp_raw: rec.timestampRaw,
      timestamp_inferred: rec.timestampInferred,
      type: mediaKind || (rec.document ? 'document' : 'chat'),
      document_name: (documentFile && documentFile.filename) || documentName,
      // Raw bytes; turned into text (docx/pdf) for the Markdown.
      document: documentFile,
      // What the page said about it, so a document missing here can still be
      // found on disk later (ticket-parser looks in Downloads for it).
      document_info: rec.document
        ? {
            name: documentName,
            truncated: Boolean(rec.document.truncated),
            type: rec.document.type || '',
            size: sizeRange(rec.document.size),
          }
        : null,
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
      `, dates read ${dateOrder.order}${dateOrder.guessed ? ' (guessed — check the dates)' : ''}` +
      (skippedOwn ? `, skipped ${skippedOwn} of your own` : '') +
      (stats.inferredStamps ? `, ${stats.inferredStamps} time(s) inferred from the bubble` : '') +
      (stats.viaCanvasCount ? `, ${stats.viaCanvasCount} image(s) read via canvas` : '') +
      (stats.documentsFound ? `, ${stats.documentsFound} document(s) found` : '') +
      (documentsMissing.length ? ` (${documentsMissing.length} not downloaded in WhatsApp, so contents missing)` : '') +
      (voiceNotesFound ? `, ${voiceNotesFound} voice note(s) found` : '') +
      (stats.imagesFailed ? `, ${stats.imagesFailed} image(s) unreadable` : '') +
      (voiceNotesUnmatched ? `, ${voiceNotesUnmatched} voice note(s) unmatched` : '')
  );
  if (stats.firstImageError) report(`first image error: ${stats.firstImageError}`);

  // The same facts, as notices the popup shows as alerts rather than burying
  // them in the log: { level: 'warning'|'info', title, detail }.
  const notices = [];
  if (documentsMissing.length) {
    notices.push({
      level: 'warning',
      title: `${documentsMissing.length} document(s) without their contents`,
      detail:
        `${documentsMissing.join(', ')} — not downloaded in WhatsApp in this tab. ` +
        'If you downloaded them before, `ticket-parser import` fills them in from your ' +
        'Downloads folder. Otherwise click their download in WhatsApp and read again.',
    });
  }
  if (voiceNotesUnmatched) {
    notices.push({
      level: 'warning',
      title: `${voiceNotesUnmatched} voice note(s) missing their audio`,
      detail: 'The audio could not be matched to the message. Reload the WhatsApp tab and read again.',
    });
  }
  if (imagesUnreadable) {
    notices.push({
      level: 'warning',
      title: `${imagesUnreadable} screenshot(s) could not be read`,
      detail: stats.firstImageError || 'The image was not available in the page.',
    });
  }
  const undated = messages.filter((m) => !m.timestamp).length;
  if (undated) {
    notices.push({
      level: 'warning',
      title: `${undated} message(s) without a date`,
      detail: 'No dated message next to them to infer it from; they are marked "date unknown".',
    });
  }
  if (dateOrder.guessed) {
    notices.push({
      level: 'warning',
      title: 'Day/month order guessed',
      detail:
        `No date in this range settles it (no day above 12), so ${dateOrder.order} was assumed. ` +
        'Check a date in the export.',
    });
  }
  if (skippedOwn) {
    notices.push({
      level: 'info',
      title: `${skippedOwn} of your own message(s) left out`,
      detail: 'Tick "include messages I sent" to keep them.',
    });
  }
  return { messages, notices };
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
    if (!selectionModeActive()) {
      return {
        ok: false,
        error:
          'No message checkboxes found. In WhatsApp, open the chat menu → "Select messages", ' +
          'tick the first and the last message to export, then try again.',
      };
    }

    const session = {
      records: new Map(), // data-id -> full message record, caught during the scroll
      anchors: new Map(), // data-id -> { dateParts, clock, sender, fromMe } for every row with metadata
      voiceNoteDurations: new Map(), // data-id -> Set<seconds>, caught during the scroll
      timeline: [], // every data-id seen, in chat order (mergeIntoTimeline)
      selectedIds: new Set(), // the ticked ones: the range's ends
      dateState: { dayFirstEvidence: 0, monthFirstEvidence: 0 },
      stats: {
        inferredStamps: 0,
        viaCanvasCount: 0,
        imagesFailed: 0,
        documentsFound: 0,
        firstImageError: '',
      },
      includeOwn: Boolean(request.includeOwn),
    };

    await loadRange(report, session);

    if (session.selectedIds.size < 2) {
      return {
        ok: false,
        error:
          session.selectedIds.size === 0
            ? 'Found no ticked message. Tick the first and the last message of the range in ' +
              'WhatsApp\'s "Select messages" mode (if you did, WhatsApp may have changed its ' +
              'markup — check SELECTORS in content.js).'
            : 'Found only one ticked message, all the way to the start of the chat. Tick both ' +
              'the first and the last message of the range.',
      };
    }

    // One last catch for anything still visible right now that the per-pass
    // scans didn't already record.
    await captureEverything(session, { extend: false });
    if (session.voiceNoteDurations.size) {
      await sleep(AUDIO_SETTLE_MS);
    }

    const { messages, notices } = buildMessages(session, report);

    if (!messages.length) {
      return {
        ok: false,
        error: session.includeOwn
          ? 'Nothing to export between the two ticked messages.'
          : 'Nothing to export between the two ticked messages (your own messages are ' +
            'excluded — tick "include messages I sent" to keep them).',
      };
    }
    return { ok: true, messages, notices };
  } catch (err) {
    return { ok: false, error: String(err && err.message ? err.message : err) };
  }
});
