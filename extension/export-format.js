'use strict';

/**
 * Turns scraped messages into the editable export:
 *
 *   conversation.md     one section per message, meant to be edited by hand
 *   media/…             screenshots, voice notes and documents it links to
 *
 * The Markdown is the hand-off format: a person can drop messages, fix text
 * and add notes, a post-processing agent can read it as-is, and
 * `ticket-parser import` reads it back (src/ticket_parser/markdown_export.py).
 * Keep the two in step: the `<!-- msg {...} -->` marker, the `> Note:`
 * blocks, the ```document fence, the media/ links, the ⚠️ lines and the
 * backslash escaping below are the contract between them.
 *
 * Loaded by popup.html only (an extension page), which is why pdf.js can run
 * here with its worker: content scripts cannot load one.
 */

const EXPORT_FORMAT_VERSION = 1;
const MAX_DOCUMENT_TEXT = 200000;

// ---------------------------------------------------------------------------
// bytes helpers

function base64ToBytes(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

const utf8 = new TextEncoder();

// ---------------------------------------------------------------------------
// reading documents

/** Minimal zip reader: enough to pull word/document.xml out of a .docx. */
async function readZipEntry(bytes, wanted) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let eocd = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i -= 1) {
    if (view.getUint32(i, true) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd === -1) throw new Error('not a zip file');

  const count = view.getUint16(eocd + 10, true);
  let offset = view.getUint32(eocd + 16, true);
  const decoder = new TextDecoder();
  for (let n = 0; n < count; n += 1) {
    if (view.getUint32(offset, true) !== 0x02014b50) break;
    const method = view.getUint16(offset + 10, true);
    const compressedSize = view.getUint32(offset + 20, true);
    const nameLength = view.getUint16(offset + 28, true);
    const extraLength = view.getUint16(offset + 30, true);
    const commentLength = view.getUint16(offset + 32, true);
    const localOffset = view.getUint32(offset + 42, true);
    const name = decoder.decode(bytes.subarray(offset + 46, offset + 46 + nameLength));
    offset += 46 + nameLength + extraLength + commentLength;
    if (name !== wanted) continue;

    const start =
      localOffset + 30 + view.getUint16(localOffset + 26, true) + view.getUint16(localOffset + 28, true);
    const data = bytes.subarray(start, start + compressedSize);
    if (method === 0) return data;
    if (method !== 8) throw new Error(`unsupported zip compression ${method}`);
    const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }
  return null;
}

const WORD_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';

function wordParagraphText(paragraph) {
  let text = '';
  const walk = (node) => {
    for (const child of node.children) {
      if (child.namespaceURI !== WORD_NS) continue;
      switch (child.localName) {
        case 't':
          text += child.textContent;
          break;
        case 'tab':
          text += '\t';
          break;
        case 'br':
        case 'cr':
          text += '\n';
          break;
        case 'pPr':
        case 'rPr':
        case 'instrText': // field codes, not visible text
        case 'delText': // tracked deletions
          break;
        default:
          walk(child);
      }
    }
  };
  walk(paragraph);
  const isListItem = paragraph.getElementsByTagNameNS(WORD_NS, 'numPr').length > 0;
  return isListItem && text.trim() ? `- ${text}` : text;
}

function wordBlockText(container) {
  const blocks = [];
  for (const child of container.children) {
    if (child.namespaceURI !== WORD_NS) continue;
    if (child.localName === 'p') {
      blocks.push(wordParagraphText(child));
    } else if (child.localName === 'tbl') {
      for (const row of child.getElementsByTagNameNS(WORD_NS, 'tr')) {
        const cells = [...row.children]
          .filter((cell) => cell.localName === 'tc')
          .map((cell) => wordBlockText(cell).replace(/\n+/g, ' ').trim());
        blocks.push(`| ${cells.join(' | ')} |`);
      }
    } else if (child.localName === 'sdt') {
      const content = child.getElementsByTagNameNS(WORD_NS, 'sdtContent')[0];
      if (content) blocks.push(wordBlockText(content));
    }
  }
  return blocks.join('\n');
}

async function docxText(bytes) {
  const xml = await readZipEntry(bytes, 'word/document.xml');
  if (!xml) throw new Error('no word/document.xml inside');
  const doc = new DOMParser().parseFromString(new TextDecoder().decode(xml), 'application/xml');
  const body = doc.getElementsByTagNameNS(WORD_NS, 'body')[0];
  if (!body) throw new Error('unreadable document.xml');
  return wordBlockText(body);
}

/** The PDF's text layer. No OCR: a scanned PDF simply has none. */
async function pdfText(bytes) {
  const pdfjs = window.pdfjsLib;
  if (!pdfjs) throw new Error('pdf.js did not load');
  pdfjs.GlobalWorkerOptions.workerSrc = 'vendor/pdf.worker.min.js';
  const pdf = await pdfjs.getDocument({ data: bytes, isEvalSupported: false, disableFontFace: true }).promise;
  const pages = [];
  try {
    for (let number = 1; number <= pdf.numPages; number += 1) {
      const page = await pdf.getPage(number);
      const content = await page.getTextContent();
      let text = '';
      for (const item of content.items) {
        if (typeof item.str !== 'string') continue;
        text += item.str;
        if (item.hasEOL) text += '\n';
      }
      pages.push(text.trim());
    }
  } finally {
    pdf.destroy();
  }
  return pages.filter(Boolean).join('\n\n');
}

/**
 * Plain text of a document, or '' when it has none we can read.
 * Never throws: a document we cannot read is still linked in the export.
 */
async function extractDocumentText(doc) {
  const name = String(doc.filename || '').toLowerCase();
  const bytes = base64ToBytes(doc.base64);
  const isPdf = name.endsWith('.pdf') || /pdf/.test(doc.mimetype || '') || bytes[0] === 0x25;
  try {
    let text = '';
    if (isPdf) text = await pdfText(bytes);
    else if (name.endsWith('.docx') || /wordprocessingml/.test(doc.mimetype || '')) text = await docxText(bytes);
    else if (/\.(txt|md|csv|log|json|xml)$/.test(name)) text = new TextDecoder().decode(bytes);
    else return { text: '', error: '' };

    text = text.replace(/\r\n?/g, '\n').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
    if (text.length > MAX_DOCUMENT_TEXT) text = `${text.slice(0, MAX_DOCUMENT_TEXT)}\n[… truncated]`;
    return { text, error: '' };
  } catch (err) {
    return { text: '', error: String(err && err.message ? err.message : err) };
  }
}

// ---------------------------------------------------------------------------
// Markdown

const pad = (n) => String(n).padStart(2, '0');

function displayTime(timestamp) {
  if (!timestamp) return 'date unknown';
  const d = new Date(timestamp * 1000);
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** File names that survive the downloads API, zip, and a Markdown link. */
function exportSafeName(value, fallback) {
  const cleaned = String(value || '')
    .normalize('NFC')
    .replace(/[\\/:*?"<>|#%()[\]\u0000-\u001f]+/g, '_')
    .replace(/\s+/g, '_')
    .replace(/^\.+/, '')
    .slice(0, 120);
  return cleaned || fallback;
}

function shortId(id) {
  const parts = String(id).split('_');
  return exportSafeName(parts[parts.length - 1], 'msg');
}

// A text line that would otherwise be read back as structure is escaped
// with a leading backslash. markdown_export.py strips exactly one.
const STRUCTURAL_LINE = /^(#|> ?note:|`{3}|⚠️|<!--|!\[|\[🎤|\[📄|\\)/i;
const escapeTextLine = (line) => (STRUCTURAL_LINE.test(line) ? `\\${line}` : line);

function fenceFor(text) {
  const longest = Math.max(0, ...(text.match(/`+/g) || []).map((run) => run.length));
  return '`'.repeat(Math.max(3, longest + 1));
}

const EDIT_GUIDE = `<!--
This file is the export. Edit it freely before running \`ticket-parser import\`:

- Drop a message: delete its whole section, from its "## " heading to the next one.
- Fix or trim a message: edit the text under its heading.
- Comment: add a blockquote starting with "Note:" inside a message, e.g.
    > Note: happens only on Android, see ticket #42
  Notes are kept apart from what was actually said, and passed to the agents.
- A section you add with your own "## " heading becomes a note on the message above it.
- Keep each "<!-- msg … -->" line: it is how a section is matched back to its message.
- Text inside a \`\`\`document fence is the attached document's content; trim it if you like.

When you are done, change "status: draft" to "status: ready": \`ticket-parser import\`
and \`watch\` skip drafts in the drop folder. (\`ticket-parser import --from <this folder>\`
takes this one export as-is.)
-->`;

/**
 * Build the export from the scraped messages.
 *
 * Returns { markdown, files } where `files` are { path, bytes, mimetype } under
 * media/. Document text is extracted here, with progress reported through
 * `report`.
 */
async function buildExport(messages, { exportedAt = new Date(), report = () => {} } = {}) {
  const files = [];
  const sections = [];
  const authors = new Set();
  let documentsRead = 0;
  const notices = [];

  for (const message of messages) {
    const id = shortId(message.id);
    const body = [];

    const marker = {
      id: message.id,
      chat: message.chat_id,
      author: message.author_name,
      ts: message.timestamp,
      inferred: Boolean(message.timestamp_inferred),
      from_me: Boolean(message.from_me),
    };
    // Lets `ticket-parser documents` / `import` find a missing document on disk.
    if (message.document_info && !(message.document && message.document.base64)) {
      marker.doc = message.document_info;
    }
    if (!message.from_me) authors.add(message.author_name);

    const text = String(message.text || '').replace(/\r\n?/g, '\n').trim();
    if (text) body.push(text.split('\n').map(escapeTextLine).join('\n'));

    if (message.media && message.media.base64) {
      const path = `media/${id}${message.media.extension || '.bin'}`;
      files.push({ path, bytes: base64ToBytes(message.media.base64), mimetype: message.media.mimetype });
      const isAudio = /^audio\//.test(message.media.mimetype || '');
      body.push(isAudio ? `[🎤 Voice note](${path})` : `![screenshot](${path})`);
    }

    if (message.document && message.document.base64) {
      const name = message.document.filename || message.document_name || 'document';
      const path = `media/${id}-${exportSafeName(name, 'document')}`;
      files.push({ path, bytes: base64ToBytes(message.document.base64), mimetype: message.document.mimetype });
      body.push(`[📄 ${name.replace(/[[\]]/g, '')}](${path})`);

      report(`reading ${name}…`);
      const { text: docText, error } = await extractDocumentText(message.document);
      if (docText) {
        documentsRead += 1;
        const fence = fenceFor(docText);
        body.push(`${fence}document\n${docText}\n${fence}`);
      } else if (error) {
        body.push(`⚠️ could not read the text of "${name}": ${error}`);
        notices.push({ level: 'warning', title: `Could not read "${name}"`, detail: `${error}. The file itself is still in the export.` });
      } else if (/\.pdf$/i.test(name) || /pdf/.test(message.document.mimetype || '')) {
        // No text and no error: a scanned PDF. No OCR, by design.
        notices.push({ level: 'info', title: `"${name}" has no text`, detail: 'Probably a scan (there is no OCR); only the file is linked.' });
      }
    }

    if (message.media_error) body.push(`⚠️ ${String(message.media_error).replace(/\n/g, ' ')}`);

    const heading =
      `## ${message.timestamp_inferred ? '~' : ''}${displayTime(message.timestamp)} · ` +
      `${message.from_me ? 'me' : message.author_name}`;
    sections.push([heading, `<!-- msg ${JSON.stringify(marker)} -->`, '', ...body.flatMap((b) => [b, ''])].join('\n').trimEnd());
  }

  const dated = messages.map((m) => m.timestamp).filter(Boolean);
  const span = dated.length
    ? `${displayTime(Math.min(...dated)).split(' ')[0]} → ${displayTime(Math.max(...dated)).split(' ')[0]}`
    : 'undated';
  const chat = (messages.find((m) => m.chat_id) || {}).chat_id || '';

  const frontMatter = [
    '---',
    `ticket-parser: ${EXPORT_FORMAT_VERSION}`,
    'status: draft',
    `chat: ${chat}`,
    `exported_at: ${exportedAt.toISOString()}`,
    `messages: ${messages.length}`,
    '---',
  ].join('\n');

  const title = `# WhatsApp — ${[...authors].join(', ') || 'conversation'} — ${span}`;
  const markdown = `${frontMatter}\n\n${title}\n\n${EDIT_GUIDE}\n\n${sections.join('\n\n')}\n`;
  return { markdown, files, documentsRead, notices };
}

function exportName(messages, exportedAt = new Date()) {
  const stamps = messages.map((m) => m.timestamp).filter(Boolean);
  const first = stamps.length ? new Date(Math.min(...stamps) * 1000) : exportedAt;
  const ymd = (d) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;
  // The export time is part of the name so a second export never overwrites
  // a conversation.md you have already started editing.
  return (
    `whatsapp-${ymd(first)}T${pad(first.getHours())}${pad(first.getMinutes())}` +
    `-exported-${ymd(exportedAt)}T${pad(exportedAt.getHours())}${pad(exportedAt.getMinutes())}${pad(exportedAt.getSeconds())}`
  );
}

// ---------------------------------------------------------------------------
// zip (stored, no compression: the media is already compressed, the Markdown
// is small, and it keeps this dependency-free)

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

/** entries: [{ path, bytes }] -> Blob (application/zip) */
function makeZip(entries, when = new Date()) {
  const dosTime = (when.getHours() << 11) | (when.getMinutes() << 5) | (when.getSeconds() >> 1);
  const dosDate = ((when.getFullYear() - 1980) << 9) | ((when.getMonth() + 1) << 5) | when.getDate();
  const chunks = [];
  const central = [];
  let offset = 0;

  for (const entry of entries) {
    const name = utf8.encode(entry.path);
    const crc = crc32(entry.bytes);
    const size = entry.bytes.length;

    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true);
    local.setUint16(6, 0x0800, true); // UTF-8 names
    local.setUint16(8, 0, true);
    local.setUint16(10, dosTime, true);
    local.setUint16(12, dosDate, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, size, true);
    local.setUint32(22, size, true);
    local.setUint16(26, name.length, true);
    local.setUint16(28, 0, true);
    chunks.push(local, name, entry.bytes);

    const record = new DataView(new ArrayBuffer(46));
    record.setUint32(0, 0x02014b50, true);
    record.setUint16(4, 20, true);
    record.setUint16(6, 20, true);
    record.setUint16(8, 0x0800, true);
    record.setUint16(10, 0, true);
    record.setUint16(12, dosTime, true);
    record.setUint16(14, dosDate, true);
    record.setUint32(16, crc, true);
    record.setUint32(20, size, true);
    record.setUint32(24, size, true);
    record.setUint16(28, name.length, true);
    record.setUint32(42, offset, true);
    central.push(record, name);

    offset += 30 + name.length + size;
  }

  const centralSize = central.reduce((sum, part) => sum + part.byteLength, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, entries.length, true);
  end.setUint16(10, entries.length, true);
  end.setUint32(12, centralSize, true);
  end.setUint32(16, offset, true);
  return new Blob([...chunks, ...central, end], { type: 'application/zip' });
}

/** Everything that goes into the export, conversation.md last. */
function exportEntries(built) {
  return [...built.files, { path: 'conversation.md', bytes: utf8.encode(built.markdown), mimetype: 'text/markdown' }];
}
