import { vi } from 'vitest';

export type Route = (init: RequestInit | undefined, url: string) => Response | Promise<Response>;

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

export function sse(events: unknown[], status = 200, done = true, headers: Record<string, string> = {}): Response {
  const enc = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const e of events) controller.enqueue(enc.encode(`data: ${typeof e === 'string' ? e : JSON.stringify(e)}\n\n`));
      if (done) controller.enqueue(enc.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return new Response(stream, { status, headers: { 'content-type': 'text/event-stream', ...headers } });
}

export function chunk(content: string, model = 'gpt-5.6-terra') {
  return { object: 'chat.completion.chunk', model, choices: [{ index: 0, delta: { content } }] };
}

export function usage(prompt: number, completion: number, model = 'gpt-5.6-terra') {
  return { object: 'chat.completion.chunk', model, choices: [], usage: { prompt_tokens: prompt, completion_tokens: completion } };
}

export function mockDoor(routes: Record<string, Route>) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const method = (init?.method ?? 'GET').toUpperCase();
    const route = routes[`${method} ${url.split('?')[0] ?? url}`];
    if (!route) return Promise.resolve(json({ error: { message: `no route ${method} ${url}` } }, 404));
    return Promise.resolve(route(init, url));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

export const READY_HEALTH = {
  version: '0.9.0',
  backend: 'codex',
  ready: true,
  model: 'gpt-5.6-terra',
  model_ready: true,
  model_hint: '',
  backends: {
    codex: { kind: 'cli', ready: true, cli_present: true, login: 'ok', command: 'codex', hint: 'ok' },
    'claude-code': {
      kind: 'cli',
      ready: false,
      cli_present: true,
      login: 'not logged in',
      command: 'claude',
      hint: 'CLI is not logged in. Run `claude` in a terminal.',
    },
    copilot: { kind: 'cli', ready: false, cli_present: false, command: 'copilot', hint: 'copilot CLI not found on PATH.' },
    openrouter: { kind: 'api', ready: false, key_present: false, hint: 'OpenRouter key missing. Paste one in Settings.' },
  },
};

export const EMPTY_HEALTH = {
  version: '0.9.0',
  backend: 'openrouter',
  ready: false,
  backends: {
    codex: { kind: 'cli', ready: false, cli_present: false, command: 'codex', hint: 'codex CLI not found on PATH.' },
    openrouter: { kind: 'api', ready: false, key_present: false, hint: 'OpenRouter key missing. Paste one in Settings.' },
  },
};

export const CODEX_MODELS = {
  backend: 'codex',
  default_model: 'gpt-5.6-terra',
  models: [
    { id: 'gpt-5.6-terra', status: 'cli' },
    { id: 'gpt-5.6-luna', status: 'cli' },
    { id: 'catalog-only-model', status: 'unverified' },
  ],
};

export const BRIDGE_KEY = { present: true, key: 'test-bridge-key' };

export function bodyOf(init: RequestInit | undefined): unknown {
  return typeof init?.body === 'string' ? JSON.parse(init.body) : null;
}
