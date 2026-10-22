import { describe, expect, it, vi } from 'vitest';
import { BRIDGE_KEY, bodyOf, CODEX_MODELS, READY_HEALTH, chunk, json, mockDoor, sse, usage } from '../test/door';
import { BACKEND_HEADER, createLiveApi } from './live';
import { ApiError } from './types';

function door(chat: () => Response) {
  return mockDoor({
    'GET /health': () => json(READY_HEALTH),
    'GET /dashboard/models': () => json(CODEX_MODELS),
    'GET /dashboard/bridge-key': () => json(BRIDGE_KEY),
    'POST /v1/chat/completions': chat,
  });
}

describe('live api', () => {
  it('lists only models the backend can run, tagged with the backend', async () => {
    door(() => sse([]));
    const api = createLiveApi();
    const models = await api.listModels('codex');
    expect(models).toEqual([
      { id: 'gpt-5.6-terra', backend: 'codex', source: 'cli' },
      { id: 'gpt-5.6-luna', backend: 'codex', source: 'cli' },
    ]);
  });

  it('falls back to the checked default model when the backend lists none', async () => {
    mockDoor({
      'GET /health': () => json(READY_HEALTH),
      'GET /dashboard/models': () => json({ default_model: 'gpt-5.6-terra', models: [{ id: 'x', status: 'unverified' }] }),
    });
    const api = createLiveApi();
    expect(await api.listModels('codex')).toEqual([{ id: 'gpt-5.6-terra', backend: 'codex', source: 'default' }]);
  });

  it('streams a reply over SSE with real usage and same-origin relative URLs', async () => {
    const fetchMock = door(() => sse([chunk('Hel'), chunk('lo'), usage(9, 20)]));
    const api = createLiveApi();
    await api.getStatus();
    const deltas: string[] = [];
    const r = await api.chat({
      backend: 'codex',
      model: 'gpt-5.6-terra',
      messages: [{ role: 'user', content: 'hi' }],
      onDelta: (t) => deltas.push(t),
    });
    expect(deltas).toEqual(['Hel', 'Hello']);
    expect(r.text).toBe('Hello');
    expect(r.backend).toBe('codex');
    expect(r.completionTokens).toBe(20);
    expect(r.promptTokens).toBe(9);
    expect(r).toMatchObject({ backend: 'codex', fellBackFrom: null });
    expect(r.tokensPerSec).toBeCloseTo(20 / (r.totalMs / 1000));

    for (const [url] of fetchMock.mock.calls) expect(typeof url === 'string' && url.startsWith('/')).toBe(true);
    const call = fetchMock.mock.calls.find(([u]) => u === '/v1/chat/completions');
    const init = call?.[1];
    const headers = init?.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer test-bridge-key');
    expect(headers).not.toHaveProperty(BACKEND_HEADER);
    expect(bodyOf(init)).toMatchObject({ model: 'gpt-5.6-terra', stream: true });
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it('names the fallback backend that answered from the X-OS3-Backend response header', async () => {
    door(() => sse([chunk('ok')], 200, true, { [BACKEND_HEADER]: 'claude-code' }));
    const api = createLiveApi();
    await api.getStatus();
    const r = await api.chat({ backend: 'codex', model: 'gpt-5.6-terra', messages: [] });
    expect(r).toMatchObject({ backend: 'claude-code', fellBackFrom: 'codex' });
  });

  it('treats a stream that ends without [DONE] as incomplete, not done', async () => {
    door(() => sse([chunk('Hal'), usage(3, 1)], 200, false));
    const api = createLiveApi();
    await api.getStatus();
    const deltas: string[] = [];
    await expect(
      api.chat({ backend: 'codex', model: 'gpt-5.6-terra', messages: [], onDelta: (t) => deltas.push(t) }),
    ).rejects.toMatchObject({ code: 'stream_incomplete', message: 'The reply was cut off before it finished.' });
    expect(deltas).toEqual(['Hal']);
  });

  it('sends max_tokens only when the caller sets it', async () => {
    const fetchMock = door(() => sse([chunk('ok')]));
    const api = createLiveApi();
    await api.getStatus();
    await api.chat({ backend: 'codex', model: 'gpt-5.6-terra', messages: [], maxTokens: 64 });
    await api.chat({ backend: 'codex', model: 'gpt-5.6-terra', messages: [] });
    const bodies = fetchMock.mock.calls.filter(([u]) => u === '/v1/chat/completions').map(([, init]) => bodyOf(init));
    expect(bodies[0]).toMatchObject({ max_tokens: 64 });
    expect(bodies[1]).not.toHaveProperty('max_tokens');
  });

  it('reports no tok/s when the upstream sends no usage', async () => {
    door(() => sse([chunk('ok')]));
    const api = createLiveApi();
    await api.getStatus();
    const r = await api.chat({ backend: 'codex', model: 'gpt-5.6-terra', messages: [] });
    expect(r.completionTokens).toBeNull();
    expect(r.tokensPerSec).toBeNull();
  });

  it('turns an error body into an ApiError with the door hint', async () => {
    door(() =>
      json({ error: { message: 'Codex usage limit reached.', code: 'upstream_429', hint: 'Wait for the plan to reset.' } }, 429),
    );
    const api = createLiveApi();
    await api.getStatus();
    const err = await api.chat({ backend: 'codex', model: 'gpt-5.6-terra', messages: [] }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 429, message: 'Codex usage limit reached.', hint: 'Wait for the plan to reset.' });
  });

  it('refuses a backend the door is not serving instead of silently rerouting', async () => {
    const fetchMock = door(() => sse([chunk('x')]));
    const api = createLiveApi();
    await api.getStatus();
    await expect(api.chat({ backend: 'openrouter', model: 'm', messages: [] })).rejects.toMatchObject({
      code: 'backend_not_active',
    });
    expect(fetchMock.mock.calls.some(([u]) => u === '/v1/chat/completions')).toBe(false);
  });

  it('sends a provider key only to /dashboard/config and never stores it', async () => {
    const fetchMock = mockDoor({
      'POST /dashboard/config': () => json({ ok: true, backend: 'openrouter', restart: 'Restart to apply.', restart_command: './os3-bridge restart' }),
    });
    const r = await createLiveApi().connect({ backend: 'openrouter', apiKey: 'sk-test-not-real' });
    expect(r).toEqual({ backend: 'openrouter', restartNote: 'Restart to apply.', restartCommand: './os3-bridge restart', activated: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(bodyOf(fetchMock.mock.calls[0]?.[1])).toEqual({ backend: 'openrouter', api_key: 'sk-test-not-real' });
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it('writes a key without switching backend when asked (activate: false)', async () => {
    const fetchMock = mockDoor({
      'POST /dashboard/config': () =>
        json({ ok: true, backend: 'codex', activated: 'no', key_written: 'yes', model_note: 'Backend and model unchanged.' }),
    });
    const r = await createLiveApi().connect({ backend: 'openai', apiKey: 'sk-test-not-real', activate: false });
    expect(bodyOf(fetchMock.mock.calls[0]?.[1])).toEqual({ backend: 'openai', api_key: 'sk-test-not-real', activate: false });
    expect(r).toMatchObject({ backend: 'codex', activated: false });
  });

  it('reads the dashboard error envelope with code and hint', async () => {
    mockDoor({
      'POST /dashboard/config': () =>
        json({ ok: false, error: 'OpenAI rejected the key (HTTP 401).', code: 'key_rejected', hint: 'Paste a working key.' }, 400),
    });
    await expect(createLiveApi().connect({ backend: 'openai', apiKey: 'sk-test-not-real' })).rejects.toMatchObject({
      status: 400,
      code: 'key_rejected',
      message: 'OpenAI rejected the key (HTTP 401).',
      hint: 'Paste a working key.',
    });
  });

  it('bench: null when the door has no /dashboard/bench yet', async () => {
    mockDoor({ 'POST /dashboard/bench': () => json({ detail: 'Method Not Allowed' }, 405) });
    expect(await createLiveApi().bench({ targets: [{ backend: 'codex', model: 'm' }], prompt: 'hi' })).toBeNull();
  });

  it('bench: maps the door rows, keeping null tok/s as null', async () => {
    const fetchMock = mockDoor({
      'POST /dashboard/bench': () =>
        json({
          ok: true,
          started_at: '2026-10-08T07:45:00Z',
          rows: [
            { backend: 'openrouter', kind: 'rest', model: 'a', ok: true, total_ms: 912, input_tokens: 14, output_tokens: 2, tok_per_s: 2.2, error: null },
            { backend: 'codex', kind: 'cli', model: 'b', ok: true, total_ms: 6340, input_tokens: null, output_tokens: null, tok_per_s: null, error: null },
            { backend: 'openai', kind: 'rest', model: 'c', ok: false, total_ms: 0, error: { code: 'backend_not_ready', hint: 'OpenAI key missing.' } },
          ],
        }),
    });
    const rows = await createLiveApi().bench({
      targets: [
        { backend: 'openrouter', model: 'a' },
        { backend: 'codex', model: 'b' },
        { backend: 'openai', model: 'c' },
      ],
      prompt: 'hi',
    });
    expect(rows).toEqual([
      { backend: 'openrouter', model: 'a', ok: true, totalMs: 912, inputTokens: 14, outputTokens: 2, tokensPerSec: 2.2, error: null },
      { backend: 'codex', model: 'b', ok: true, totalMs: 6340, inputTokens: null, outputTokens: null, tokensPerSec: null, error: null },
      { backend: 'openai', model: 'c', ok: false, totalMs: 0, inputTokens: null, outputTokens: null, tokensPerSec: null, error: { code: 'backend_not_ready', hint: 'OpenAI key missing.' } },
    ]);
    expect(bodyOf(fetchMock.mock.calls[0]?.[1])).toMatchObject({ prompt: 'hi', max_tokens: 64 });
  });

  it('explains an unreachable door', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))));
    await expect(createLiveApi().getStatus()).rejects.toMatchObject({ code: 'door_unreachable' });
  });
});
