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

browser.runtime.onMessage.addListener(async (request, sender) => {
  if (request.command === 'export-bundle') {
    try {
      const result = await saveBundle(request.base64, request.filename);
      return { ok: true, ...result };
    } catch (err) {
      return { ok: false, error: String(err && err.message ? err.message : err) };
    }
  }

  if (request.command === 'progress') {
    // Relayed from the content script to the popup; nothing to do here.
    return undefined;
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
