import type { BackendState, BackendStatus, DoorStatus, FallbackInfo } from './types';

export const BACKEND_LABELS: Record<string, string> = {
  chatgpt: 'ChatGPT plan',
  codex: 'Codex',
  'claude-code': 'Claude Code',
  copilot: 'GitHub Copilot',
  'grok-build': 'Grok Build',
  antigravity: 'Antigravity',
  openrouter: 'OpenRouter',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  xai: 'xAI',
  nous: 'Nous Portal',
};

const ORDER = Object.keys(BACKEND_LABELS);

export const STATE_LABELS: Record<BackendState, string> = {
  ready: 'Ready',
  not_set_up: 'Not set up',
  cli_missing: 'CLI missing',
  not_logged_in: 'Not logged in',
  out_of_quota: 'Out of quota',
};

export function backendLabel(id: string): string {
  return BACKEND_LABELS[id] ?? id;
}

export interface HealthBody {
  version?: string;
  backend?: string;
  ready?: boolean;
  model?: string;
  model_ready?: boolean | null;
  model_hint?: string;
  upstream_status?: number;
  ui_api?: number;
  fallback?: { enabled?: unknown; chain?: unknown; active?: unknown; cooling?: unknown };
  backends?: Record<string, BackendRow | undefined>;
}

export interface BackendRow {
  name?: string;
  state?: string;
  kind?: string;
  role?: string;
  ready?: boolean;
  key_present?: boolean;
  key_source?: string | null;
  key_mode?: string | null;
  cli_present?: boolean;
  login?: string;
  hint?: string;
  command?: string;
}

const FILE_PATH = /(?:^|[\s'"(=:])(?:~|\/(?:Users|home|root|private|var|tmp|opt|etc|Library))\/|\b[A-Za-z]:\\/;

function text(value: unknown, fallback: string): string {
  if (typeof value !== 'string') return fallback;
  const v = value.trim();
  return v && v !== 'ok' && !FILE_PATH.test(v) ? v : fallback;
}

function keyHint(id: string): string {
  const env = KEY_ENV[id];
  return env ? `Set ${env}, or save a key in Settings.` : 'Save a key in Settings.';
}

const CONTRACT_STATES: Record<string, BackendState | undefined> = {
  ready: 'ready',
  not_configured: 'not_set_up',
  cli_missing: 'cli_missing',
  not_logged_in: 'not_logged_in',
  out_of_quota: 'out_of_quota',
};

function readyHint(label: string, row: BackendRow, active: boolean): string {
  if (row.kind === 'cli') return `Signed in through the ${label} CLI.`;
  return active ? `${label} key on file.` : `Key on file. Not checked until ${label} is the active backend.`;
}

function rowState(id: string, row: BackendRow, active: boolean): { state: BackendState; hint: string } {
  const label = row.name ?? backendLabel(id);
  if (typeof row.state === 'string') {
    const state = CONTRACT_STATES[row.state] ?? 'not_set_up';
    if (row.state === 'not_configured' && row.kind !== 'cli') return { state, hint: keyHint(id) };
    return { state, hint: state === 'ready' ? readyHint(label, row, active) : text(row.hint, 'Run ./doctor.sh for the cause.') };
  }
  if (row.kind === 'cli') {
    const command = row.command ?? id;
    if (row.cli_present === false || row.login === 'unchecked') {
      return { state: 'cli_missing', hint: text(row.hint, `Install the official ${command} CLI.`) };
    }
    if (row.ready !== true) {
      return { state: 'not_logged_in', hint: text(row.hint, `Sign in with \`${command}\` in a terminal.`) };
    }
    return { state: 'ready', hint: readyHint(label, row, active) };
  }
  if (row.key_present !== true) return { state: 'not_set_up', hint: keyHint(id) };
  if (row.ready !== true) {
    return { state: 'not_set_up', hint: text(row.hint, `Add a ${label} API key in Settings.`) };
  }
  return { state: 'ready', hint: readyHint(label, row, active) };
}

function activeState(
  base: { state: BackendState; hint: string },
  health: HealthBody,
  kind: 'cli' | 'api',
): { state: BackendState; hint: string } {
  if (base.state !== 'ready') return base;
  const hint = text(health.model_hint, '');
  if (health.upstream_status === 429) {
    return { state: 'out_of_quota', hint: hint || 'The upstream says this plan is out of quota. Wait, or switch backend.' };
  }
  if (health.upstream_status === 401 || health.upstream_status === 403) {
    return kind === 'cli'
      ? { state: 'not_logged_in', hint: hint || 'The CLI login was refused. Sign in again in a terminal.' }
      : { state: 'not_set_up', hint: hint || 'The provider refused this key. Paste a working key in Settings.' };
  }
  if (health.model_ready === false) {
    return { state: 'not_set_up', hint: hint || `Model '${health.model ?? ''}' is not available here.` };
  }
  if (health.model_ready === null && hint) {
    return { state: 'ready', hint: `Unconfirmed: ${hint}` };
  }
  return base;
}

export function deriveStatus(health: HealthBody): DoorStatus {
  const active = typeof health.backend === 'string' ? health.backend : '';
  const rows = health.backends ?? {};
  const ids = Object.keys(rows).sort((a, b) => rank(a) - rank(b));
  const backends: BackendStatus[] = [];
  for (const id of ids) {
    const row = rows[id];
    if (!row) continue;
    const kind = row.kind === 'cli' ? 'cli' : 'api';
    const base = rowState(id, row, id === active);
    const { state, hint } = id === active ? activeState(base, health, kind) : base;
    const status: BackendStatus = { id, label: row.name ?? backendLabel(id), kind, state, hint, active: id === active };
    if (kind === 'api') {
      status.keySource = row.key_source === 'env' || row.key_source === 'file' ? row.key_source : null;
      status.keyFileSaved = typeof row.key_mode === 'string' && row.key_mode !== '';
    }
    backends.push(status);
  }
  const uiApi = typeof health.ui_api === 'number' ? health.ui_api : null;
  const status: DoorStatus = { version: health.version ?? '', activeBackend: active, backends, uiApi };
  const fallback = readFallback(health.fallback);
  if (fallback) status.fallback = fallback;
  return status;
}

function readFallback(f: HealthBody['fallback']): FallbackInfo | undefined {
  if (f?.enabled !== true || !Array.isArray(f.chain)) return undefined;
  const chain = f.chain.filter((id): id is string => typeof id === 'string');
  const cooling: string[] = [];
  for (const c of Array.isArray(f.cooling) ? (f.cooling as unknown[]) : []) {
    const id = typeof c === 'object' && c !== null ? (c as { backend?: unknown }).backend : undefined;
    if (typeof id === 'string') cooling.push(id);
  }
  return chain.length > 1 ? { chain, next: typeof f.active === 'string' ? f.active : '', cooling } : undefined;
}

export function failoverTarget(status: DoorStatus): string | null {
  const f = status.fallback;
  if (!f) return null;
  const active = status.backends.find((b) => b.active);
  if (!f.cooling.includes(status.activeBackend) && active?.state !== 'out_of_quota') return null;
  if (f.next && f.next !== status.activeBackend) return f.next;
  return f.chain.find((id) => id !== status.activeBackend && !f.cooling.includes(id)) ?? null;
}

export function failoverNote(status: DoorStatus | null): string | null {
  const to = status ? failoverTarget(status) : null;
  return status && to
    ? `${backendLabel(status.activeBackend)} is cooling down after a usage limit. Requests fail over to ${backendLabel(to)}.`
    : null;
}

export const KEY_ENV: Record<string, string> = {
  openrouter: 'OPENROUTER_API_KEY',
  openai: 'OPENAI_API_KEY',
  anthropic: 'ANTHROPIC_API_KEY',
  xai: 'XAI_API_KEY',
  nous: 'NOUS_API_KEY',
};

/** The door prefers an env var over a key file, so a key saved from this UI would go unused. */
export function envKeyWarning(b: BackendStatus): string | null {
  if (b.keySource !== 'env') return null;
  const name = KEY_ENV[b.id] ?? 'An environment variable';
  return b.keyFileSaved
    ? `${name} is set in the door's environment, so the saved ${b.label} key file is ignored. Unset it and restart the door to use the saved key.`
    : `${name} is set in the door's environment. A key saved here is ignored until it is unset and the door restarts.`;
}

function rank(id: string): number {
  const i = ORDER.indexOf(id);
  return i === -1 ? ORDER.length : i;
}

export function servable(status: DoorStatus): BackendStatus[] {
  const failsOver = failoverTarget(status) !== null;
  return status.backends.filter((b) => b.active && (b.state === 'ready' || (b.state === 'out_of_quota' && failsOver)));
}

export function nothingSetUp(status: DoorStatus): boolean {
  return status.backends.every((b) => b.state !== 'ready' && b.state !== 'out_of_quota');
}
