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
  // Voice notes: WhatsApp Web renders them as an <audio> element once played,
  // but always exposes one in the DOM with the blob src set.
  audio: 'audio[src^="blob:"]',
  // The conversation pane; used to find the scroll container.
  main: '#main',
};

const SCROLL_SETTLE_MS = 900;

// ---------------------------------------------------------------------------

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
 * Scroll upwards to pull history out of the phone and into the DOM.
 * Stops early when the row count stops growing -- that is the top of what
 * WhatsApp will serve.
 */
async function loadHistory(passes, report) {
  const container = findScrollContainer();
  if (!container) return;

  let previous = document.querySelectorAll(SELECTORS.row).length;
  for (let pass = 1; pass <= passes; pass += 1) {
    container.scrollTop = 0;
    await sleep(SCROLL_SETTLE_MS);
    const current = document.querySelectorAll(SELECTORS.row).length;
    report(`scrollback ${pass}/${passes} — ${current} messages loaded`);
    if (current === previous) {
      report(`reached the top after ${pass} pass(es) — ${current} messages`);
      return;
    }
    previous = current;
  }
}

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

/**
 * Get the bytes of a voice note WhatsApp has already decrypted.
 *
 * Unlike images, there is no canvas fallback for audio -- if the blob fetch
 * fails there is nothing else to try, so the caller records a media_error
 * and keeps the message's text and metadata.
 */
async function audioToBase64(audio) {
  return blobToBase64(audio.src);
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

async function scrapeMessages({ includeOwn, report }) {
  const rows = [...document.querySelectorAll(SELECTORS.row)];
  const messages = [];
  let skippedOwn = 0;
  let imagesFailed = 0;
  let inferredStamps = 0;
  let viaCanvasCount = 0;
  let documentsFound = 0;
  let voiceNotesFound = 0;
  let voiceNotesFailed = 0;
  let firstImageError = '';
  let lastDateParts = null;
  let lastSender = '';

  for (const row of rows) {
    const dataId = row.getAttribute('data-id') || '';
    const fromMe = dataId.startsWith('true_');
    if (fromMe && !includeOwn) {
      skippedOwn += 1;
      continue;
    }

    const metaNode = row.querySelector(SELECTORS.meta);
    const meta = parseMeta(metaNode ? metaNode.dataset.prePlainText : '');

    let timestamp = meta.timestamp;
    let sender = meta.sender;
    let timestampInferred = false;

    if (timestamp) {
      const date = new Date(timestamp * 1000);
      lastDateParts = {
        year: date.getFullYear(),
        month: date.getMonth(),
        day: date.getDate(),
      };
      if (sender) lastSender = sender;
    } else {
      // Media rows: no metadata attribute, just a visible time in the bubble.
      const visible = findVisibleTime(row);
      const combined = combineDateAndTime(lastDateParts, visible);
      if (combined) {
        timestamp = combined;
        timestampInferred = true;
        inferredStamps += 1;
      }
      sender = fromMe ? 'me' : lastSender || 'unknown';
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
        if (viaCanvas) viaCanvasCount += 1;
      } catch (err) {
        imagesFailed += 1;
        const detail = String(err && err.message ? err.message : err);
        if (!firstImageError) firstImageError = detail;
        mediaError = `could not read the rendered image: ${detail}`;
      }
    }

    // Voice notes render as a plain <audio> element -- decrypted the same way
    // images are, so the same blob-fetch approach applies.
    let mediaKind = media ? 'image' : '';
    const audio = !media ? row.querySelector(SELECTORS.audio) : null;
    if (audio && audio.src) {
      try {
        const { base64, mimetype } = await audioToBase64(audio);
        media = { base64, mimetype, extension: extensionFor(mimetype) };
        mediaKind = 'audio';
        voiceNotesFound += 1;
      } catch (err) {
        voiceNotesFailed += 1;
        const detail = String(err && err.message ? err.message : err);
        mediaError = `could not read the voice note: ${detail}`;
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
        documentsFound += 1;
        if (!mediaError) {
          mediaError = `document "${documentName}" was attached; download it from WhatsApp if you need it`;
        }
      }
    }

    if (!text && !media && !mediaError) continue; // system rows, dividers

    messages.push({
      schema: 1,
      id: dataId,
      chat_id: chatIdFromDataId(dataId),
      author_id: chatIdFromDataId(dataId),
      author_name: sender || (fromMe ? 'me' : 'unknown'),
      timestamp: timestamp || 0,
      timestamp_raw: meta.raw,
      timestamp_inferred: timestampInferred,
      type: mediaKind || (documentName ? 'document' : 'chat'),
      document_name: documentName,
      text,
      from_me: fromMe,
      media,
      media_error: mediaError,
      source: 'firefox-extension',
    });
  }

  report(
    `collected ${messages.length} messages` +
      (skippedOwn ? `, skipped ${skippedOwn} of your own` : '') +
      (inferredStamps ? `, ${inferredStamps} time(s) inferred from the bubble` : '') +
      (viaCanvasCount ? `, ${viaCanvasCount} image(s) read via canvas` : '') +
      (documentsFound ? `, ${documentsFound} document(s) noted` : '') +
      (voiceNotesFound ? `, ${voiceNotesFound} voice note(s) downloaded` : '') +
      (imagesFailed ? `, ${imagesFailed} image(s) unreadable` : '') +
      (voiceNotesFailed ? `, ${voiceNotesFailed} voice note(s) unreadable` : '')
  );
  if (firstImageError) report(`first image error: ${firstImageError}`);
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
    await loadHistory(Number(request.scrollbacks) || 0, report);
    const messages = await scrapeMessages({
      includeOwn: Boolean(request.includeOwn),
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
