'use strict';

/**
 * Runs in the PAGE's own JS realm -- declared in manifest.json with
 * `"world": "MAIN"` -- so it can see calls WhatsApp's own code makes to
 * standard browser APIs.
 *
 * Voice notes have no <audio> element, and WhatsApp never runs a received
 * one through AudioContext.decodeAudioData either (that path turned out to
 * only be used when *sending* a new recording, to draw its waveform before
 * upload). But scrolling through history *does* make WhatsApp create a
 * `URL.createObjectURL(blob)` for each voice note's decrypted audio as its
 * row loads -- confirmed by watching it happen: one audio/ogg blob per
 * voice note, standard Web API, nothing WhatsApp-internal depended on here.
 *
 * This hooks that call and keeps every audio-typed blob it sees. Since nothing
 * in the DOM ties a blob back to the row it came from, each one is decoded
 * once, right here, purely to read its `.duration` -- content.js matches that
 * against the duration WhatsApp displays on each row's progress slider. This
 * is *our own* decode of bytes we already have, not a hook on WhatsApp's
 * decode path, so it works regardless of whether WhatsApp itself ever calls
 * decodeAudioData for this blob.
 *
 * Communication with content.js (the isolated-world script) is a plain JSON
 * string written onto `window`, read from there via `window.wrappedJSObject`
 * -- see content.js for why that's the right tool for a MAIN-world script
 * handing structured data to an isolated-world one.
 *
 * A voice note re-scrolled past (which happens routinely -- content.js keeps
 * re-scanning until every note's duration is matched, see
 * ensureEveryVoiceNoteDurationIsCaptured there) triggers another
 * createObjectURL call for the same audio, so entries are deduplicated by
 * byte length here. That makes `captured.length` a reliable ground truth for
 * "how many distinct voice notes exist in what's loaded so far" -- content.js
 * uses exactly that count to know when it can stop looking.
 *
 * Nothing here ever removes an entry once captured, and content.js does not
 * ask it to: a clip content.js matches to a row is only spliced out of *its
 * own* copy of the list, not this one, so the same clip is available again
 * for a later export of the same conversation. This is deliberate --
 * WhatsApp appears to call createObjectURL for a given voice note only once
 * per page load, reusing the cached blob on later scrolls past the same row
 * rather than recreating it. An earlier version of this file cleared
 * `captured` at the start of every export to avoid a stale clip being
 * wrongly matched to an unrelated row in a later export; that was the wrong
 * tradeoff -- it permanently lost any clip only ever captured once, early in
 * the session, which is far worse than the rare risk of a duration
 * collision across exports.
 */
(function () {
  const LOG = '[ticket-parser]';

  if (window.__ticketParserVoiceHookInstalled) return;
  window.__ticketParserVoiceHookInstalled = true;

  const captured = [];

  function refreshLog() {
    window.__ticketParserAudioLogJSON = JSON.stringify(captured);
  }

  function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(binary);
  }

  const originalCreateObjectURL = URL.createObjectURL.bind(URL);
  URL.createObjectURL = function ticketParserCreateObjectURL(blob) {
    const url = originalCreateObjectURL(blob);
    try {
      const isAudio = typeof Blob !== 'undefined' && blob instanceof Blob && (blob.type || '').startsWith('audio/');
      if (isAudio) {
        blob
          .arrayBuffer()
          .then((buffer) => {
            if (captured.some((entry) => entry.byteLength === buffer.byteLength)) {
              // Already have this one (a re-scroll past a row already
              // captured) -- skip the decode, not just the push.
              return;
            }
            const base64 = arrayBufferToBase64(buffer);
            // Decode our own copy purely to read the duration -- WhatsApp's
            // own code never has to touch AudioContext for this to work.
            const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            return audioCtx
              .decodeAudioData(buffer.slice(0))
              .then((decoded) => {
                audioCtx.close();
                if (captured.some((entry) => entry.byteLength === buffer.byteLength)) {
                  return; // lost a race with another capture of the same clip
                }
                captured.push({
                  durationSec: Math.round(decoded.duration),
                  base64,
                  mimetype: blob.type,
                  byteLength: buffer.byteLength,
                });
                refreshLog();
                console.log(
                  LOG,
                  'captured voice note:', Math.round(decoded.duration) + 's', buffer.byteLength + 'b',
                  '-- total distinct:', captured.length
                );
              })
              .catch((err) => {
                audioCtx.close();
                console.error(LOG, 'could not decode a captured audio blob for its duration', err);
              });
          })
          .catch((err) => console.error(LOG, 'could not read a captured audio blob', err));
      }
    } catch (err) {
      console.error(LOG, 'createObjectURL hook failed', err);
    }
    return url;
  };

  refreshLog();
  console.log(LOG, 'voice-note capture (createObjectURL hook) ready');
})();
