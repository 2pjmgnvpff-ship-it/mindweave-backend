// sse.js — POST-based Server-Sent-Events reader (fetch + ReadableStream).
// EventSource cannot POST or send headers, so we parse `event:` / `data:` frames ourselves.

/**
 * Incremental SSE parser. Feed it text chunks of any size; it emits complete events.
 * Handles \n, \r\n and \r line endings (including a \r\n split across chunks),
 * multi-line `data:` fields, comments (`:`), and missing trailing blank line at end().
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
    if (line.charCodeAt(0) === 58 /* ':' */) return; // comment / keep-alive
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
        if (i >= len) break; // no complete line yet
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

/**
 * Read a fetch Response body as SSE and call onEvent per event.
 * Resolves when the stream ends; rejects on network errors (AbortError when aborted).
 */
export async function readSseResponse(response, onEvent) {
  const parser = createSseParser(onEvent);
  const body = response.body;
  if (!body || typeof body.getReader !== 'function') {
    parser.push(await response.text());
    parser.end();
    return;
  }
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8');
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.end();
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* ignore */
    }
  }
}

export const CHAT_EVENTS = ['delta', 'citation', 'whisper', 'perspective', 'suggestion', 'crisis', 'done', 'error'];
