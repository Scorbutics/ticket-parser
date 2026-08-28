'use strict';

const statusEl = document.getElementById('status');
const runButton = document.getElementById('run');
const saveButton = document.getElementById('save');
const shareButton = document.getElementById('share');
const deliveryEl = document.getElementById('delivery');

// Held between the read and the send, because navigator.share must be called
// from a fresh click: awaiting a 90-second scrape burns the user activation
// that the share sheet requires. Hence two steps rather than one button.
let bundle = null;

function show(text, isError = false) {
  statusEl.textContent = text;
  statusEl.className = isError ? 'error' : '';
}

function append(text) {
  statusEl.textContent += (statusEl.textContent ? '\n' : '') + text;
}

browser.runtime.onMessage.addListener((request) => {
  if (request.command === 'progress') append(request.text);
});

async function gzip(text) {
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream('gzip'));
  return await new Response(stream).blob();
}

async function blobToBase64(blob) {
  const buffer = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < buffer.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, buffer.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

function bundleName(messages) {
  const stamps = messages.map((m) => m.timestamp).filter(Boolean);
  const when = stamps.length ? new Date(Math.min(...stamps) * 1000) : new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return (
    `whatsapp-${when.getFullYear()}${pad(when.getMonth() + 1)}${pad(when.getDate())}` +
    `T${pad(when.getHours())}${pad(when.getMinutes())}.json.gz`
  );
}

runButton.addEventListener('click', async () => {
  runButton.disabled = true;
  deliveryEl.style.display = 'none';
  bundle = null;
  show('Reading the conversation…');

  try {
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    if (!tab || !/^https:\/\/web\.whatsapp\.com/.test(tab.url || '')) {
      show('Open web.whatsapp.com in this tab first.', true);
      return;
    }

    const scraped = await browser.tabs.sendMessage(tab.id, {
      command: 'scrape',
      scrollbacks: Number(document.getElementById('scrollbacks').value) || 0,
      includeOwn: document.getElementById('includeOwn').checked,
    });

    if (!scraped || !scraped.ok) {
      show(scraped ? scraped.error : 'No response from the page.', true);
      return;
    }

    const payload = {
      schema: 1,
      source: 'firefox-extension',
      exported_at: Math.floor(Date.now() / 1000),
      message_count: scraped.messages.length,
      messages: scraped.messages,
    };
    const blob = await gzip(JSON.stringify(payload));
    bundle = { blob, filename: bundleName(scraped.messages) };

    append(
      `bundled ${scraped.messages.length} messages → ${bundle.filename} ` +
        `(${(blob.size / 1024).toFixed(0)} KB)`
    );
    deliveryEl.style.display = 'block';
  } catch (err) {
    show(String(err && err.message ? err.message : err), true);
  } finally {
    runButton.disabled = false;
  }
});

saveButton.addEventListener('click', async () => {
  if (!bundle) return;
  saveButton.disabled = true;
  try {
    const result = await browser.runtime.sendMessage({
      command: 'export-bundle',
      base64: await blobToBase64(bundle.blob),
      filename: bundle.filename,
    });
    if (result && result.ok) {
      append(`saved to Downloads/ticket-parser/${bundle.filename}`);
    } else {
      append(`save failed: ${result ? result.error : 'no response'}`);
    }
  } finally {
    saveButton.disabled = false;
  }
});

shareButton.addEventListener('click', async () => {
  if (!bundle) return;
  const file = new File([bundle.blob], bundle.filename, { type: 'application/gzip' });

  if (!navigator.canShare || !navigator.canShare({ files: [file] })) {
    append(
      'This browser will not share files. Use "Save bundle" instead, then ' +
        'attach it to an email yourself.'
    );
    return;
  }

  try {
    await navigator.share({
      files: [file],
      title: 'WhatsApp bug reports',
      text: bundle.filename,
    });
    append('handed to the share sheet');
  } catch (err) {
    const message = String(err && err.message ? err.message : err);
    if (!/abort/i.test(message)) append(`share failed: ${message}`);
  }
});
