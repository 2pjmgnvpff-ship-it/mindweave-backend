// sse.js — POST-based Server-Sent-Events reader (fetch + ReadableStream).
// BC3 BUG-CC-6c: enforces a per-read idle watchdog (default 25_000 ms) that resets on
// every received byte chunk (including `: keep-alive\n\n` heartbeats), avoiding silent hangs.

export const DEFAULT_SSE_IDLE_MS = 25_000;

/**
 * Incremental SSE parser. Handles \n, \r\n and \r line endings (including \r\n split across
 * chunks), multi-line `data:` fields, comments (`:`), and missing trailing blank line at end().
 * @param {(evt: {event: string, data: any, raw: string, id?: string}) => void} onEvent
 */
export function createSseParser(onEvent) {
  let buffer = '';
  let eventName = '';
  let dataLines = [];
  let lastId;
  let hasData = false;

  function dispatch() {
    if (!hasData) {
      eventName = '';
      return;
    }
    const raw = dataLines.join('\n');
    let data = raw;
    try {
      data = JSON.parse(raw);
    } catch {
      /* keep raw string */
    }
    const evt = { event: eventName || 'message', data, raw };
    if (lastId !== undefined) evt.id = lastId;
    eventName = '';
    dataLines = [];
    hasData = false;
    onEvent(evt);
  }

  function processLine(line) {
    if (line === '') {
      dispatch();
      return;
    }
    if (line.charCodeAt(0) === 58 /* ':' */) return; // comment / keep-alive heartbeat
    const idx = line.indexOf(':');
    const field = idx === -1 ? line : line.slice(0, idx);
    let value = idx === -1 ? '' : line.slice(idx + 1);
    if (value.charCodeAt(0) === 32) value = value.slice(1);
    if (field === 'event') eventName = value;
    else if (field === 'data') {
      dataLines.push(value);
      hasData = true;
    } else if (field === 'id') lastId = value;
  }

  return {
    push(chunk) {
      buffer += chunk;
      for (;;) {
        let i = 0;
        const len = buffer.length;
        while (i < len) {
          const c = buffer.charCodeAt(i);
          if (c === 10 || c === 13) break;
          i++;
        }
        if (i >= len) break;
        const c = buffer.charCodeAt(i);
        if (c === 13 && i === len - 1) break; // lone \r at end: wait to see if \n follows
        const line = buffer.slice(0, i);
        const skip = c === 13 && buffer.charCodeAt(i + 1) === 10 ? 2 : 1;
        buffer = buffer.slice(i + skip);
        processLine(line);
      }
    },
    end() {
      if (buffer.length) {
        const rest = buffer.replace(/\r$/, '');
        buffer = '';
        processLine(rest);
      }
      dispatch();
    },
  };
}

function makeIdleError(ms) {
  const err = new Error(`SSE stream idle timeout (${ms}ms)`);
  err.name = 'SseIdleTimeoutError';
  err.code = 'SSE_IDLE_TIMEOUT';
  return err;
}

function makeAbortError() {
  const err = new Error('The operation was aborted.');
  err.name = 'AbortError';
  return err;
}

/**
 * Read a fetch Response body as SSE and invoke `onEvent` per event.
 * Resets the per-read idle watchdog on every received chunk (including comments/heartbeats).
 */
export async function readSseResponse(response, onEvent, { idleTimeoutMs = DEFAULT_SSE_IDLE_MS, signal } = {}) {
  const parser = createSseParser(onEvent);
  const body = response?.body;
  if (!body || typeof body.getReader !== 'function') {
    if (signal?.aborted) throw makeAbortError();
    parser.push(await response.text());
    parser.end();
    return;
  }
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8');
  let idleTimer = null;

  const readChunkWithWatchdog = () => {
    if (signal?.aborted) return Promise.reject(makeAbortError());
    const useWatchdog = Number.isFinite(idleTimeoutMs) && idleTimeoutMs > 0;
    if (!useWatchdog && !signal) return reader.read();

    return new Promise((resolve, reject) => {
      let settled = false;
      const cleanup = () => {
        if (idleTimer !== null) {
          clearTimeout(idleTimer);
          idleTimer = null;
        }
        signal?.removeEventListener('abort', onAbort);
      };
      const onAbort = () => {
        if (settled) return;
        settled = true;
        cleanup();
        reader.cancel('aborted').catch(() => {});
        reject(makeAbortError());
      };
      if (signal) signal.addEventListener('abort', onAbort, { once: true });
      if (useWatchdog) {
        idleTimer = setTimeout(() => {
          if (settled) return;
          settled = true;
          cleanup();
          reader.cancel('sse_idle_timeout').catch(() => {});
          reject(makeIdleError(idleTimeoutMs));
        }, idleTimeoutMs);
      }
      reader.read().then(
        (res) => {
          if (settled) return;
          settled = true;
          cleanup();
          resolve(res);
        },
        (err) => {
          if (settled) return;
          settled = true;
          cleanup();
          reject(err);
        },
      );
    });
  };

  try {
    for (;;) {
      const { done, value } = await readChunkWithWatchdog();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.end();
  } finally {
    if (idleTimer !== null) {
      clearTimeout(idleTimer);
      idleTimer = null;
    }
    try {
      reader.releaseLock();
    } catch {
      /* ignore */
    }
  }
}

export const CHAT_EVENTS = ['agent_step', 'delta', 'citation', 'action_proposal', 'whisper', 'perspective', 'suggestion', 'crisis', 'done', 'error'];
