import { describe, expect, it } from 'vitest';
import { sseData } from './sse';

function streamOf(parts: string[]) {
  const enc = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(c) {
      for (const p of parts) c.enqueue(enc.encode(p));
      c.close();
    },
  });
}

async function collect(parts: string[]) {
  const out: string[] = [];
  for await (const d of sseData(streamOf(parts))) out.push(d);
  return out;
}

describe('sseData', () => {
  it('reassembles events split across chunks and stops at [DONE]', async () => {
    expect(await collect(['data: {"a"', ':1}\n', '\ndata: two\n\n', 'data: [DONE]\n\n', 'data: after\n\n'])).toEqual([
      '{"a":1}',
      'two',
    ]);
  });

  it('handles CRLF, comments and non-data fields', async () => {
    expect(await collect([': ping\r\n\r\nevent: x\r\ndata: one\r\n\r\n'])).toEqual(['one']);
  });

  it('returns true only when [DONE] arrived, so a cut-off stream is detectable', async () => {
    const end = async (parts: string[]) => {
      const events = sseData(streamOf(parts));
      for (;;) {
        const next = await events.next();
        if (next.done) return next.value;
      }
    };
    expect(await end(['data: a\n\n', 'data: [DONE]\n\n'])).toBe(true);
    expect(await end(['data: a\n\ndata: [DONE]'])).toBe(true);
    expect(await end(['data: a\n\n'])).toBe(false);
    expect(await end([])).toBe(false);
  });

  it('yields a trailing event without a blank line', async () => {
    expect(await collect(['data: tail'])).toEqual(['tail']);
  });
});
