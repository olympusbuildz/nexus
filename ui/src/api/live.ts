import { sseData } from './sse';
import { backendLabel, deriveStatus } from './status';
import {
  ApiError,
  BENCH_MAX_TOKENS,
  type ChatRequest,
  type ChatResult,
  type BenchRequest,
  type BenchRow,
  type ConnectRequest,
  type ConnectResult,
  type DoorStatus,
  type NexusApi,
  type RunnableModel,
} from './types';

/** Response header naming the fallback backend that served a reply. Absent on a normal reply; never sent. */
export const BACKEND_HEADER = 'X-OS3-Backend';

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

async function readJson(res: Response): Promise<unknown> {
  try {
    return (await res.json()) as unknown;
  } catch {
    return null;
  }
}

export function errorFromBody(status: number, body: unknown, fallback: string): ApiError {
  if (isObject(body)) {
    const inner = body.error;
    if (isObject(inner)) {
      return new ApiError(str(inner.message) || fallback, {
        status,
        code: str(inner.code) || str(inner.type) || 'error',
        hint: str(inner.hint),
      });
    }
    if (typeof inner === 'string' && inner) {
      return new ApiError(inner, { status, code: str(body.code) || `http_${String(status)}`, hint: str(body.hint) });
    }
    const detail = str(body.detail);
    if (detail) return new ApiError(detail, { status, code: `http_${String(status)}`, hint: '' });
  }
  return new ApiError(fallback, {
    status,
    code: `http_${status}`,
    hint: status >= 500 ? 'The door failed. Run ./doctor.sh for the cause.' : '',
  });
}

const UNREACHABLE = new ApiError('Cannot reach the door at this address.', {
  code: 'door_unreachable',
  hint: 'Start it with ./nexus start, then reload this page.',
});

async function request(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(path, { cache: 'no-store', ...init });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw UNREACHABLE;
  }
}

async function getJson(path: string): Promise<unknown> {
  const res = await request(path);
  const body = await readJson(res);
  if (!res.ok) throw errorFromBody(res.status, body, `GET ${path} failed (${res.status}).`);
  return body;
}

function postInit(body: unknown, extra?: Record<string, string>, signal?: AbortSignal): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...extra },
    body: JSON.stringify(body),
    signal,
  };
}

export function createLiveApi(): NexusApi {
  let bridgeKey: string | null = null;
  let activeBackend = '';

  async function key(): Promise<string> {
    if (bridgeKey) return bridgeKey;
    const body = await getJson('/dashboard/bridge-key');
    const value = isObject(body) && body.present === true ? str(body.key) : '';
    if (!value) {
      throw new ApiError('This door has no bridge key yet.', {
        code: 'no_bridge_key',
        hint: 'Run ./nexus setup once to create it.',
      });
    }
    bridgeKey = value;
    return value;
  }

  async function openChat(req: ChatRequest): Promise<Response> {
    const payload = {
      model: req.model,
      messages: req.messages,
      stream: true,
      stream_options: { include_usage: true },
      ...(req.maxTokens !== undefined && { max_tokens: req.maxTokens }),
    };
    const send = async () =>
      request(
        '/v1/chat/completions',
        postInit(payload, { Authorization: `Bearer ${await key()}` }, req.signal),
      );
    let res = await send();
    if (res.status === 401) {
      bridgeKey = null;
      res = await send();
    }
    return res;
  }

  return {
    mode: 'live',

    async getStatus(): Promise<DoorStatus> {
      const body = await getJson('/health');
      const status = deriveStatus(isObject(body) ? body : {});
      activeBackend = status.activeBackend;
      return status;
    },

    async listModels(backend: string): Promise<RunnableModel[]> {
      const body = await getJson(`/dashboard/models?backend=${encodeURIComponent(backend)}`);
      if (!isObject(body)) return [];
      const rows = Array.isArray(body.models) ? body.models : [];
      const listed: RunnableModel[] = [];
      for (const row of rows) {
        if (!isObject(row)) continue;
        const id = str(row.id);
        const source = str(row.status);
        if (id && (source === 'live' || source === 'cli')) {
          listed.push({ id, backend: str(row.backend) || backend, source });
        }
      }
      if (listed.length) return listed;
      const fallback = str(body.default_model);
      return fallback ? [{ id: fallback, backend, source: 'default' }] : [];
    },

    async chat(req: ChatRequest): Promise<ChatResult> {
      if (activeBackend && req.backend !== activeBackend) {
        throw new ApiError(`${backendLabel(req.backend)} is not the backend this door is serving.`, {
          code: 'backend_not_active',
          hint: `Switch to ${backendLabel(req.backend)} in Settings, or pick a ${backendLabel(activeBackend)} model.`,
        });
      }
      const started = performance.now();
      const res = await openChat(req);
      if (!res.ok) {
        throw errorFromBody(res.status, await readJson(res), `The door answered ${res.status}.`);
      }
      const served = res.headers.get(BACKEND_HEADER);
      const fellBackFrom = served && served !== req.backend ? req.backend : null;
      let text = '';
      let firstTokenMs: number | null = null;
      const usage: { prompt: number | null; completion: number | null } = { prompt: null, completion: null };
      let model = req.model;

      const take = (chunk: Json) => {
        if (isObject(chunk.error)) throw errorFromBody(res.status, chunk, 'The upstream failed mid-reply.');
        model = str(chunk.model) || model;
        if (isObject(chunk.usage)) {
          usage.prompt = num(chunk.usage.prompt_tokens) ?? usage.prompt;
          usage.completion = num(chunk.usage.completion_tokens) ?? usage.completion;
        }
        const choices = Array.isArray(chunk.choices) ? chunk.choices : [];
        for (const choice of choices) {
          if (!isObject(choice)) continue;
          const part = isObject(choice.delta) ? str(choice.delta.content) : isObject(choice.message) ? str(choice.message.content) : '';
          if (!part) continue;
          firstTokenMs ??= performance.now() - started;
          text += part;
          req.onDelta?.(text);
        }
      };

      const type = res.headers.get('content-type') ?? '';
      if (type.includes('text/event-stream') && res.body) {
        const events = sseData(res.body);
        let complete = false;
        try {
          for (;;) {
            const next = await events.next();
            if (next.done) {
              complete = next.value;
              break;
            }
            let chunk: unknown;
            try {
              chunk = JSON.parse(next.value);
            } catch {
              continue;
            }
            if (isObject(chunk)) take(chunk);
          }
        } finally {
          await events.return(false);
        }
        if (!complete) {
          throw new ApiError(text ? 'The reply was cut off before it finished.' : 'The stream ended before any reply.', {
            code: 'stream_incomplete',
            hint: 'The stream closed without [DONE], so this reply is incomplete. Try again; ./doctor.sh shows whether the backend is healthy.',
          });
        }
      } else {
        const body = await readJson(res);
        if (isObject(body)) take(body);
      }

      const totalMs = performance.now() - started;
      if (!text) {
        throw new ApiError('The model returned an empty reply.', {
          code: 'empty_reply',
          hint: 'Try again, or pick another model. ./doctor.sh shows whether the backend is healthy.',
        });
      }
      const tokens = usage.completion;
      return {
        text,
        backend: fellBackFrom && served ? served : req.backend,
        fellBackFrom,
        model,
        firstTokenMs,
        totalMs,
        promptTokens: usage.prompt,
        completionTokens: tokens,
        tokensPerSec: tokens !== null && totalMs > 0 ? tokens / (totalMs / 1000) : null,
      };
    },

    async bench(req: BenchRequest): Promise<BenchRow[] | null> {
      const res = await request(
        '/dashboard/bench',
        postInit({ targets: req.targets, prompt: req.prompt, max_tokens: BENCH_MAX_TOKENS }, undefined, req.signal),
      );
      // Planned in docs/ui-api.md: a door without it answers 404/405, which means "not yet", not an error.
      if (res.status === 404 || res.status === 405) return null;
      const body = await readJson(res);
      if (!res.ok || !isObject(body) || body.ok !== true) throw errorFromBody(res.status, body, 'The bench did not run.');
      const rows = Array.isArray(body.rows) ? body.rows : [];
      return rows.filter(isObject).map((r) => {
        const err = isObject(r.error) ? r.error : null;
        return {
          backend: str(r.backend),
          model: str(r.model),
          ok: r.ok === true,
          totalMs: num(r.total_ms) ?? 0,
          inputTokens: num(r.input_tokens),
          outputTokens: num(r.output_tokens),
          tokensPerSec: num(r.tok_per_s),
          error: err ? { code: str(err.code), hint: str(err.hint) } : null,
        };
      });
    },

    async connect(req: ConnectRequest): Promise<ConnectResult> {
      const payload: Record<string, string | boolean> = { backend: req.backend };
      if (req.apiKey) payload.api_key = req.apiKey;
      if (req.activate === false) payload.activate = false;
      const res = await request('/dashboard/config', postInit(payload));
      const body = await readJson(res);
      if (!res.ok || !isObject(body) || body.ok !== true) {
        throw errorFromBody(res.status, body, 'The door did not save that choice.');
      }
      const warning = str(body.warning);
      return {
        backend: str(body.backend) || req.backend,
        restartNote: str(body.restart),
        restartCommand: str(body.restart_command),
        activated: body.activated !== 'no',
        ...(warning ? { warning } : {}),
      };
    },
  };
}
