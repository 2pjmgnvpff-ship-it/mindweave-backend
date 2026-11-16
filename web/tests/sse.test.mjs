import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createSseParser, readSseResponse } from '../scripts/api/sse.js';

function collect() {
  const events = [];
  const parser = createSseParser((e) => events.push(e));
  return { events, parser };
}

test('parses a simple event', () => {
  const { events, parser } = collect();
  parser.push('event: delta\ndata: {"text":"你好"}\n\n');
  assert.deepEqual(events.map((e) => [e.event, e.data]), [['delta', { text: '你好' }]]);
});

test('chunk boundaries split mid-field, mid-line and mid-event', () => {
  const full = 'event: delta\ndata: {"text":"她最近"}\n\nevent: citation\ndata: {"index":1,"materialId":"m1","title":"膝盖疼","happenedOn":"2026-05-12"}\n\nevent: done\ndata: {"messageId":"x"}\n\n';
  for (let size = 1; size <= 7; size++) {
    const { events, parser } = collect();
    for (let i = 0; i < full.length; i += size) parser.push(full.slice(i, i + size));
    parser.end();
    assert.deepEqual(events.map((e) => e.event), ['delta', 'citation', 'done'], `chunk size ${size}`);
    assert.equal(events[1].data.title, '膝盖疼');
  }
});

test('CRLF line endings, including \\r\\n split across chunks', () => {
  const { events, parser } = collect();
  parser.push('event: whisper\r');
  parser.push('\ndata: {"text":"轻声"}\r\n\r');
  parser.push('\n');
  assert.equal(events.length, 1);
  assert.equal(events[0].event, 'whisper');
  assert.equal(events[0].data.text, '轻声');
});

test('multi-line data is joined with newlines', () => {
  const { events, parser } = collect();
  parser.push('event: suggestion\ndata: line one\ndata: line two\n\n');
  assert.equal(events[0].data, 'line one\nline two');
  const json = collect();
  json.parser.push('data: {"items":\ndata: ["a","b"]}\n\n');
  assert.deepEqual(json.events[0].data, { items: ['a', 'b'] });
  assert.equal(json.events[0].event, 'message');
});

test('comments and keep-alives are ignored; event name resets after dispatch', () => {
  const { events, parser } = collect();
  parser.push(': ping\n\nevent: delta\ndata: {"text":"a"}\n\ndata: {"text":"b"}\n\n');
  assert.deepEqual(events.map((e) => e.event), ['delta', 'message']);
});

test('end() flushes a final event without trailing blank line', () => {
  const { events, parser } = collect();
  parser.push('event: done\ndata: {"messageId":"z"}');
  assert.equal(events.length, 0);
  parser.end();
  assert.equal(events[0].event, 'done');
});

test('readSseResponse works with a ReadableStream body split in bytes (multi-byte UTF-8)', async () => {
  const bytes = new TextEncoder().encode('event: delta\ndata: {"text":"丘比特在听"}\n\n');
  const stream = new ReadableStream({
    start(ctrl) {
      for (let i = 0; i < bytes.length; i += 3) ctrl.enqueue(bytes.slice(i, i + 3));
      ctrl.close();
    },
  });
  const events = [];
  await readSseResponse({ body: stream }, (e) => events.push(e));
  assert.equal(events[0].data.text, '丘比特在听');
});
