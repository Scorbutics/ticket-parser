'use strict';

/**
 * A view over the export job, which runs in background.js.
 *
 * The popup is destroyed whenever it loses focus, so it holds nothing that
 * matters: reopening it asks the background for the current state (a read in
 * progress, or the last finished export) and carries on from there.
 *
 * What needs attention is shown as alerts (#notices: the job's errors,
 * warnings and result; #toasts: outcomes of clicks here, like a save). The
 * raw progress log stays available under "Details".
 */

const statusEl = document.getElementById('status');
const detailsEl = document.getElementById('details');
const noticesEl = document.getElementById('notices');
const toastsEl = document.getElementById('toasts');
const lastEl = document.getElementById('last');
const runButton = document.getElementById('run');
const saveButton = document.getElementById('save');
const shareButton = document.getElementById('share');
const saveZipButton = document.getElementById('saveZip');
const deliveryEl = document.getElementById('delivery');
const includeOwnEl = document.getElementById('includeOwn');

const OPTIONS_KEY = 'popupOptions';
const ICONS = { error: '⛔', warning: '⚠️', info: 'ℹ️', success: '✅', progress: '⏳' };

// navigator.share must be called straight from a click, so the zip is fetched
// from the background ahead of time, as soon as an export is known to exist.
let shareFile = null; // File, for lastExport.name
let shareFileName = '';
let lastStatus = '';
let latestLine = '';

function noticeElement({ level, title, detail }, onClose) {
  const box = document.createElement('div');
  box.className = `notice ${level}`;
  box.setAttribute('role', level === 'error' ? 'alert' : 'status');

  const icon = document.createElement('span');
  icon.className = 'icon';
  icon.textContent = ICONS[level] || '';
  box.append(icon);

  const text = document.createElement('div');
  text.className = 'text';
  const strong = document.createElement('strong');
  strong.textContent = title;
  text.append(strong);
  if (detail) text.append(document.createTextNode(detail));
  box.append(text);

  if (onClose) {
    const close = document.createElement('button');
    close.className = 'close';
    close.title = 'Dismiss';
    close.textContent = '×';
    close.addEventListener('click', onClose);
    box.append(close);
  }
  return box;
}

/** A transient alert for something done from this popup. */
function toast(level, title, detail = '') {
  const box = noticeElement({ level, title, detail }, () => box.remove());
  toastsEl.append(box);
  if (level === 'success' || level === 'info') setTimeout(() => box.remove(), 6000);
}

function setLog(lines) {
  statusEl.textContent = lines.join('\n');
  statusEl.scrollTop = statusEl.scrollHeight;
}

function appendLog(text) {
  statusEl.textContent += (statusEl.textContent ? '\n' : '') + text;
  statusEl.scrollTop = statusEl.scrollHeight;
  latestLine = text;
  const progress = noticesEl.querySelector('.notice.progress .text');
  if (progress) progress.lastChild.textContent = text;
}

function timeOf(ms) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function prefetchShareFile(name) {
  if (shareFileName === name) return;
  shareFileName = name;
  shareFile = null;
  const result = await browser.runtime.sendMessage({ command: 'get-export-zip' });
  if (result && result.ok && result.name === name) {
    const bytes = Uint8Array.from(atob(result.base64), (c) => c.charCodeAt(0));
    shareFile = new File([bytes], `${name}.zip`, { type: 'application/zip' });
  }
}

function renderNotices(state) {
  noticesEl.replaceChildren();
  const add = (notice) => noticesEl.append(noticeElement(notice));

  if (state.status === 'running') {
    add({ level: 'progress', title: 'Reading…', detail: latestLine || 'Starting.' });
    return;
  }
  if (state.status === 'error' || state.status === 'interrupted') {
    add({
      level: 'error',
      title: state.status === 'interrupted' ? 'Read interrupted' : 'Read failed',
      detail:
        state.error +
        (state.lastExport ? ' Your previous export below is unaffected.' : ''),
    });
    return;
  }
  if (state.status === 'done') {
    const notices = state.notices || [];
    const order = { error: 0, warning: 1, info: 2 };
    if (!notices.some((n) => n.level === 'error')) {
      add({ level: 'success', title: 'Export ready', detail: state.summary || '' });
    }
    [...notices].sort((a, b) => (order[a.level] ?? 3) - (order[b.level] ?? 3)).forEach(add);
  }
}

function render(state) {
  const running = state.status === 'running';
  runButton.disabled = running;
  runButton.textContent = running ? 'Reading…' : 'Read conversation';

  const log = state.log || [];
  latestLine = log[log.length - 1] || '';
  setLog(log);
  renderNotices(state);

  // The log is the detail, not the message: open while a read runs, folded
  // once it is over -- but only on a change, so a toggle by hand sticks.
  if (state.status !== lastStatus) detailsEl.open = running;
  lastStatus = state.status;
  detailsEl.style.display = log.length ? '' : 'none';

  const last = state.lastExport;
  if (last) {
    lastEl.textContent =
      `Last export: ${last.messageCount} message(s), ${last.fileCount - 1} file(s) — ${timeOf(last.finishedAt)}` +
      (running ? ' (kept until this read finishes)' : '');
    lastEl.title = last.name;
    lastEl.style.display = 'block';
    deliveryEl.style.display = 'block';
    prefetchShareFile(last.name).catch(() => {});
  } else {
    lastEl.style.display = 'none';
    deliveryEl.style.display = 'none';
  }
}

browser.runtime.onMessage.addListener((request) => {
  if (request.command === 'job-log') appendLog(request.line);
  if (request.command === 'job-state') render(request.state);
});

async function restore() {
  try {
    const options = (await browser.storage.local.get(OPTIONS_KEY))[OPTIONS_KEY];
    if (options) includeOwnEl.checked = Boolean(options.includeOwn);
  } catch (err) {
    // Options are a convenience; the defaults in popup.html are fine.
  }
  render(await browser.runtime.sendMessage({ command: 'get-export-state' }));
}

runButton.addEventListener('click', async () => {
  toastsEl.replaceChildren();
  const options = { includeOwn: includeOwnEl.checked };
  browser.storage.local.set({ [OPTIONS_KEY]: options }).catch(() => {});

  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https:\/\/web\.whatsapp\.com/.test(tab.url || '')) {
    toast('error', 'Not on WhatsApp Web', 'Open web.whatsapp.com in this tab first.');
    return;
  }

  runButton.disabled = true;
  const started = await browser.runtime.sendMessage({ command: 'start-export', tabId: tab.id, options });
  if (!started || !started.ok) {
    toast('error', 'Could not start', started ? started.error : 'The extension did not respond.');
    runButton.disabled = false;
  }
  // From here on, progress arrives as job-log / job-state messages. Closing
  // the popup does not stop the read.
});

async function save(button, zip) {
  button.disabled = true;
  try {
    const result = await browser.runtime.sendMessage({ command: 'save-export', zip });
    if (result && result.ok) toast('success', 'Saved', result.message.replace(/^saved to /, ''));
    else toast('error', 'Save failed', result ? result.error : 'The extension did not respond.');
  } finally {
    button.disabled = false;
  }
}

saveButton.addEventListener('click', () => save(saveButton, false));
saveZipButton.addEventListener('click', () => save(saveZipButton, true));

shareButton.addEventListener('click', async () => {
  if (!shareFile) {
    toast('info', 'Still preparing the .zip', 'Try again in a second.');
    return;
  }
  if (!navigator.canShare || !navigator.canShare({ files: [shareFile] })) {
    toast('warning', 'Sharing files is not supported here', 'Use "Save as .zip", then attach it to an email yourself.');
    return;
  }

  try {
    await navigator.share({
      files: [shareFile],
      title: 'WhatsApp bug reports',
      text: shareFile.name,
    });
    toast('success', 'Handed to the share sheet');
  } catch (err) {
    const message = String(err && err.message ? err.message : err);
    if (!/abort/i.test(message)) toast('error', 'Share failed', message);
  }
});

restore().catch((err) => toast('error', 'Popup error', String(err && err.message ? err.message : err)));
