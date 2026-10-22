/**
 * Yields each `data:` payload of a server-sent-event stream, stopping at `[DONE]`.
 * Returns true only when `[DONE]` arrived: a stream that just ends was cut off.
 */
export async function* sseData(body: ReadableStream<Uint8Array>): AsyncGenerator<string, boolean> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, '\n');
      let cut = buffer.indexOf('\n\n');
      while (cut !== -1) {
        const event = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const data = eventData(event);
        if (data === '[DONE]') return true;
        if (data !== null) yield data;
        cut = buffer.indexOf('\n\n');
      }
    }
    const tail = eventData(buffer + decoder.decode());
    if (tail === '[DONE]') return true;
    if (tail !== null) yield tail;
    return false;
  } finally {
    reader.releaseLock();
  }
}

function eventData(event: string): string | null {
  const lines = event
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).replace(/^ /, ''));
  return lines.length ? lines.join('\n').trim() : null;
}
