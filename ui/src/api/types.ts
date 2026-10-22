
export type BackendState = 'ready' | 'not_set_up' | 'cli_missing' | 'not_logged_in' | 'out_of_quota';

export type BackendKind = 'cli' | 'api';

export interface BackendStatus {
  id: string;
  label: string;
  kind: BackendKind;
  state: BackendState;
  hint: string;
  active: boolean;
  keySource?: 'env' | 'file' | null;
  keyFileSaved?: boolean;
}

export interface DoorStatus {
  version: string;
  activeBackend: string;
  backends: BackendStatus[];
  uiApi: number | null;
  fallback?: FallbackInfo;
}

export interface FallbackInfo {
  chain: string[];
  next: string;
  cooling: string[];
}

export type ModelSource = 'live' | 'cli' | 'default';

export interface RunnableModel {
  id: string;
  backend: string;
  source: ModelSource;
}

export interface ChatTurn {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface ChatRequest {
  backend: string;
  model: string;
  messages: ChatTurn[];
  maxTokens?: number;
  signal?: AbortSignal;
  onDelta?: (text: string) => void;
}

export interface ChatResult {
  text: string;
  backend: string;
  /** The backend asked for, when a fallback answered instead (X-OS3-Backend). */
  fellBackFrom?: string | null;
  model: string;
  /** Time to first content token, ms. */
  firstTokenMs: number | null;
  totalMs: number;
  promptTokens: number | null;
  completionTokens: number | null;
  /** completion tokens / total seconds; null when the upstream reported no usage. */
  tokensPerSec: number | null;
}

export interface ConnectRequest {
  backend: string;
  apiKey?: string;
  activate?: boolean;
}

export interface ConnectResult {
  backend: string;
  restartNote: string;
  restartCommand: string;
  activated: boolean;
  warning?: string;
}

export const BENCH_MAX_TOKENS = 64;

export interface BenchRequest {
  targets: { backend: string; model: string }[];
  prompt: string;
  signal?: AbortSignal;
}

export interface BenchRow {
  backend: string;
  model: string;
  ok: boolean;
  totalMs: number;
  inputTokens: number | null;
  outputTokens: number | null;
  tokensPerSec: number | null;
  error: { code: string; hint: string } | null;
}

export interface NexusApi {
  readonly mode: 'live' | 'mock';
  getStatus(): Promise<DoorStatus>;
  listModels(backend: string): Promise<RunnableModel[]>;
  chat(request: ChatRequest): Promise<ChatResult>;
  connect(request: ConnectRequest): Promise<ConnectResult>;
  bench(request: BenchRequest): Promise<BenchRow[] | null>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly hint: string;

  constructor(message: string, opts: { status?: number; code?: string; hint?: string } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = opts.status ?? 0;
    this.code = opts.code ?? 'error';
    this.hint = opts.hint ?? '';
  }
}

export function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}
