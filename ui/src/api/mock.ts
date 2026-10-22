import { ApiError, type ChatRequest, type ChatResult, type DoorStatus, type NexusApi, type RunnableModel } from './types';

export const MOCK_STATUS: DoorStatus = {
  version: 'mock',
  uiApi: 1,
  activeBackend: 'codex',
  backends: [
    { id: 'codex', label: 'Codex', kind: 'cli', state: 'ready', hint: 'Signed in through the Codex CLI.', active: true },
    {
      id: 'claude-code',
      label: 'Claude Code',
      kind: 'cli',
      state: 'not_logged_in',
      hint: 'CLI is not logged in. Run `claude` in a terminal.',
      active: false,
    },
    {
      id: 'copilot',
      label: 'GitHub Copilot',
      kind: 'cli',
      state: 'cli_missing',
      hint: 'copilot CLI not found. Install the official CLI and sign in there.',
      active: false,
    },
    {
      id: 'openrouter',
      label: 'OpenRouter',
      kind: 'api',
      state: 'not_set_up',
      hint: 'Set OPENROUTER_API_KEY, or save a key in Settings.',
      active: false,
    },
  ],
};

export const MOCK_MODELS: RunnableModel[] = [
  { id: 'gpt-5.6-terra', backend: 'codex', source: 'cli' },
  { id: 'gpt-5.6-luna', backend: 'codex', source: 'cli' },
];

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(t);
      reject(new DOMException('Aborted', 'AbortError'));
    });
  });

export function createMockApi(status: DoorStatus = MOCK_STATUS, models: RunnableModel[] = MOCK_MODELS): NexusApi {
  return {
    mode: 'mock',
    getStatus: () => Promise.resolve(status),
    listModels: (backend) => Promise.resolve(models.filter((m) => m.backend === backend)),
    async chat(req: ChatRequest): Promise<ChatResult> {
      if (req.model.includes('fail')) {
        throw new ApiError('Codex usage limit reached.', {
          status: 429,
          code: 'codex_usage_limit',
          hint: 'The upstream is rate limiting or out of quota. Wait and retry, or GET /doctor.',
        });
      }
      const started = performance.now();
      const words = `Mock reply from ${req.model}. Start the door and use npm run dev for real answers.`.split(' ');
      let text = '';
      let first: number | null = null;
      for (const word of words) {
        await sleep(40, req.signal);
        text += (text ? ' ' : '') + word;
        first ??= performance.now() - started;
        req.onDelta?.(text);
      }
      const totalMs = performance.now() - started;
      return {
        text,
        backend: req.backend,
        model: req.model,
        firstTokenMs: first,
        totalMs,
        promptTokens: 12,
        completionTokens: words.length,
        tokensPerSec: words.length / (totalMs / 1000),
      };
    },
    bench: () => Promise.resolve(null),
    connect: (req) =>
      Promise.resolve({
        backend: req.activate === false ? status.activeBackend : req.backend,
        restartNote: 'Mock: nothing was saved.',
        restartCommand: '',
        activated: req.activate !== false,
      }),
  };
}
