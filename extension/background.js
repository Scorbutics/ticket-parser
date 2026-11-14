'use strict';

/**
 * Writes scraped messages to disk via the downloads API.
 *
 * One directory per message under <Downloads>/ticket-parser/, matching exactly
 * what the Python side already expects:
 *
 *   ticket-parser/<message-id>/message.json
 *   ticket-parser/<message-id>/<image file>
 *
 * No server, no port, nothing listening. `ticket-parser import` moves these
 * into data/inbox/ and the rest of the pipeline is unchanged.
 */

const DROP_FOLDER = 'ticket-parser';

function safeName(value, fallback) {
  const cleaned = String(value || '')
    .replace(/[^A-Za-z0-9._-]+/g, '_')
    .slice(0, 120);
  return cleaned && cleaned !== '.' && cleaned !== '..' ? cleaned : fallback;
}

function base64ToBlob(base64, mimetype) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mimetype || 'application/octet-stream' });
}

async function saveBlob(blob, filename) {
  // Firefox's event page has DOM APIs, but fall back to a data URL if a future
  // build takes createObjectURL away.
  let url;
  let revoke = null;
  if (typeof URL !== 'undefined' && URL.createObjectURL) {
    url = URL.createObjectURL(blob);
    revoke = () => URL.revokeObjectURL(url);
  } else {
    url = await new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.readAsDataURL(blob);
    });
  }

  try {
    await browser.downloads.download({
      url,
      filename,
      conflictAction: 'overwrite',
      saveAs: false,
    });
  } finally {
    if (revoke) setTimeout(revoke, 20000);
  }
}

async function saveMessages(messages) {
  let written = 0;
  let images = 0;

  for (const message of messages) {
    const slug = safeName(message.id, `msg-${message.timestamp || 0}`);
    const directory = `${DROP_FOLDER}/${slug}`;

    const record = { ...message };
    if (message.media) {
      const filename = `${slug}${message.media.extension || '.bin'}`;
      await saveBlob(
        base64ToBlob(message.media.base64, message.media.mimetype),
        `${directory}/${filename}`
      );
      record.media = { filename, mimetype: message.media.mimetype };
      images += 1;
    }

    const json = new Blob([JSON.stringify(record, null, 2)], {
      type: 'application/json',
    });
    await saveBlob(json, `${directory}/message.json`);
    written += 1;
  }

  return { written, images };
}

/**
 * Save the whole export as one gzipped file.
 *
 * Android has a single flat downloads folder and no reliable subdirectory
 * support, so the directory-per-message layout does not survive the move to
 * mobile. One file does -- and it is also the thing you can hand to the share
 * sheet or attach to an email.
 */
async function saveBundle(base64, filename) {
  const blob = base64ToBlob(base64, 'application/gzip');
  await saveBlob(blob, `${DROP_FOLDER}/${filename}`);
  return { written: 1, bytes: blob.size };
}

// ---------------------------------------------------------------------------
// The export job.
//
// It runs here rather than in the popup because the popup is destroyed the
// moment it loses focus -- taking a half-finished read, or a finished export
// not yet saved, with it. Here it survives the popup closing, and the last
// finished export is also written to storage.local, so it survives this
// background page being unloaded and even a Firefox restart. Reopening the
// popup just asks for the current state.
//
// Uses buildExport / exportEntries / exportName / makeZip from
// export-format.js and pdf.js, loaded ahead of this file (manifest.json).
// ---------------------------------------------------------------------------

const JOB_KEY = 'exportJob';
const EXPORT_KEY = 'lastExport';

// The current or most recent read: { status: 'idle'|'running'|'done'|'error'|
// 'interrupted', log: [], notices: [{ level, title, detail }], summary, error,
// startedAt, finishedAt }. Small; persisted. `notices` are what the popup
// shows as alerts; `log` is the detail behind them.
let job = null;
// The last *finished* export, kept apart so a read that fails or is
// interrupted never costs you it: { name, messageCount, finishedAt,
// entries: [{ path, mimetype, base64 }] }.
let lastExport = null;
let loaded = false;

function bytesToBase64(bytes) {
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

/** What the popup is shown: everything but the file contents. */
function publicState() {
  return {
    ...(job || { status: 'idle', log: [] }),
    lastExport: lastExport
      ? {
          name: lastExport.name,
          messageCount: lastExport.messageCount,
          fileCount: lastExport.entries.length,
          finishedAt: lastExport.finishedAt,
        }
      : null,
  };
}

function notifyPopup() {
  browser.runtime.sendMessage({ command: 'job-state', state: publicState() }).catch(() => {});
}

function logLine(line) {
  if (!job) return;
  job.log.push(line);
  browser.runtime.sendMessage({ command: 'job-log', line }).catch(() => {});
}

async function loadState() {
  if (loaded) return;
  loaded = true;
  try {
    const stored = await browser.storage.local.get([JOB_KEY, EXPORT_KEY]);
    lastExport = stored[EXPORT_KEY] || null;
    job = stored[JOB_KEY] || null;
    // This page was unloaded mid-read: that read is gone for good.
    if (job && job.status === 'running') {
      job.status = 'interrupted';
      job.error = 'The read was interrupted (Firefox unloaded the extension). Run it again.';
    }
  } catch (err) {
    console.error('[ticket-parser] could not load the last export', err);
  }
}

async function persist(keys) {
  try {
    const values = {};
    if (keys.includes(JOB_KEY)) values[JOB_KEY] = job;
    if (keys.includes(EXPORT_KEY)) values[EXPORT_KEY] = lastExport;
    await browser.storage.local.set(values);
  } catch (err) {
    console.error('[ticket-parser] could not store the export', err);
    if (job) {
      job.notices = job.notices || [];
      job.notices.push({
        level: 'warning',
        title: 'This export will not survive closing Firefox',
        detail: `It could not be stored (${err && err.message ? err.message : err}). Save it now.`,
      });
    }
  }
}

async function runExport(tabId, options) {
  job = { status: 'running', log: ['Reading the conversation…'], notices: [], startedAt: Date.now() };
  await persist([JOB_KEY]);
  notifyPopup();

  try {
    const scraped = await browser.tabs.sendMessage(tabId, { command: 'scrape', ...options });
    if (!scraped || !scraped.ok) throw new Error(scraped ? scraped.error : 'No response from the page.');

    const exportedAt = new Date();
    job.notices.push(...(scraped.notices || []));
    const built = await buildExport(scraped.messages, { exportedAt, report: logLine });
    job.notices.push(...(built.notices || []));
    const entries = exportEntries(built).map((entry) => ({
      path: entry.path,
      mimetype: entry.mimetype,
      base64: bytesToBase64(entry.bytes),
    }));
    const name = exportName(scraped.messages, exportedAt);
    const size = entries.reduce((sum, entry) => sum + entry.base64.length * 0.75, 0);
    logLine(
      `exported ${scraped.messages.length} messages → ${name}/conversation.md` +
        (built.files.length ? ` + ${built.files.length} file(s)` : '') +
        (built.documentsRead ? `, text of ${built.documentsRead} document(s) included` : '') +
        ` (${(size / 1024).toFixed(0)} KB)`
    );
    lastExport = { name, messageCount: scraped.messages.length, finishedAt: Date.now(), entries };
    job.summary =
      `${scraped.messages.length} message(s)` +
      (built.files.length ? `, ${built.files.length} file(s)` : '') +
      (built.documentsRead ? `, text of ${built.documentsRead} document(s)` : '') +
      ` — ${(size / 1024).toFixed(0)} KB`;
    job.status = 'done';
    job.finishedAt = Date.now();
    await persist([JOB_KEY, EXPORT_KEY]);
  } catch (err) {
    job.status = 'error';
    job.error = String(err && err.message ? err.message : err);
    job.finishedAt = Date.now();
    await persist([JOB_KEY]);
  }
  notifyPopup();
}

function zipOfLastExport() {
  const entries = lastExport.entries.map((entry) => ({ path: entry.path, bytes: base64ToBytes(entry.base64) }));
  return makeZip(entries, new Date(lastExport.finishedAt || Date.now()));
}

async function saveLastExport(asZip) {
  await loadState();
  if (!lastExport) throw new Error('no export to save yet');
  const { name, entries } = lastExport;
  if (asZip) {
    await saveBlob(zipOfLastExport(), `${DROP_FOLDER}/${name}.zip`);
    return `saved to Downloads/${DROP_FOLDER}/${name}.zip`;
  }
  // conversation.md is last, so a folder that has it is complete.
  for (const entry of entries) {
    await saveBlob(base64ToBlob(entry.base64, entry.mimetype), `${DROP_FOLDER}/${name}/${entry.path}`);
  }
  return `saved to Downloads/${DROP_FOLDER}/${name}/ — edit conversation.md, then set status: ready`;
}

browser.runtime.onMessage.addListener(async (request, sender) => {
  if (request.command === 'start-export') {
    await loadState();
    if (job && job.status === 'running') return { ok: false, error: 'A read is already running.' };
    // Not awaited: the popup may close at any time; it follows along through
    // job-log / job-state messages, or by asking again when reopened.
    runExport(request.tabId, request.options);
    return { ok: true };
  }

  if (request.command === 'get-export-state') {
    await loadState();
    return publicState();
  }

  if (request.command === 'get-export-zip') {
    // For the share sheet, which has to be called from the popup itself.
    await loadState();
    if (!lastExport) return { ok: false, error: 'no export yet' };
    return { ok: true, name: lastExport.name, base64: bytesToBase64(new Uint8Array(await zipOfLastExport().arrayBuffer())) };
  }

  if (request.command === 'save-export') {
    try {
      return { ok: true, message: await saveLastExport(Boolean(request.zip)) };
    } catch (err) {
      return { ok: false, error: String(err && err.message ? err.message : err) };
    }
  }

  if (request.command === 'progress') {
    // From the content script while it reads. Also keeps this page awake
    // during a long read.
    if (job && job.status === 'running') logLine(request.text);
    return undefined;
  }

  if (request.command === 'export-bundle') {
    try {
      const result = await saveBundle(request.base64, request.filename);
      return { ok: true, ...result };
    } catch (err) {
      return { ok: false, error: String(err && err.message ? err.message : err) };
    }
  }

  if (request.command === 'export') {
    try {
      const result = await saveMessages(request.messages);
      return { ok: true, ...result };
    } catch (err) {
      return { ok: false, error: String(err && err.message ? err.message : err) };
    }
  }

  return undefined;
});
