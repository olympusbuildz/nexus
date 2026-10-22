import { describe, expect, it } from 'vitest';
import { EMPTY_HEALTH, READY_HEALTH } from '../test/door';
import { deriveStatus, nothingSetUp, servable, envKeyWarning, failoverNote } from './status';

const stateOf = (health: Parameters<typeof deriveStatus>[0], id: string) =>
  deriveStatus(health).backends.find((b) => b.id === id);

describe('deriveStatus', () => {
  it('gives every backend exactly one honest state with a hint', () => {
    const s = deriveStatus(READY_HEALTH);
    expect(s.backends.map((b) => [b.id, b.state])).toEqual([
      ['codex', 'ready'],
      ['claude-code', 'not_logged_in'],
      ['copilot', 'cli_missing'],
      ['openrouter', 'not_set_up'],
    ]);
    for (const b of s.backends) expect(b.hint).not.toBe('');
    expect(stateOf(READY_HEALTH, 'claude-code')?.hint).toContain('claude');
    expect(servable(s).map((b) => b.id)).toEqual(['codex']);
    expect(nothingSetUp(s)).toBe(false);
  });

  it('marks the active backend out of quota on an upstream 429', () => {
    const b = stateOf({ ...READY_HEALTH, ready: false, upstream_status: 429, model_hint: 'Usage limit reached.' }, 'codex');
    expect(b?.state).toBe('out_of_quota');
    expect(b?.hint).toBe('Usage limit reached.');
  });

  it('is not ready when the configured model is unavailable', () => {
    const b = stateOf({ ...READY_HEALTH, model_ready: false, model_hint: 'Model x not in your plan.' }, 'codex');
    expect(b?.state).toBe('not_set_up');
    expect(b?.hint).toBe('Model x not in your plan.');
  });

  it('uses the contract state and name when the door sends them, unknown as not ready', () => {
    const s = deriveStatus({
      backend: 'codex',
      backends: {
        codex: { name: 'Codex', kind: 'cli', state: 'out_of_quota', ready: true, hint: 'Usage limit reached on Codex.' },
        openai: { name: 'OpenAI', kind: 'rest', state: 'not_configured', hint: 'OpenAI key missing.' },
        xai: { name: 'xAI', kind: 'rest', state: 'warming_up', ready: true, hint: 'Something new.' },
        openrouter: { name: 'OpenRouter', kind: 'rest', state: 'ready', ready: true, key_present: true, hint: 'ok' },
      },
    });
    expect(s.backends.map((b) => [b.id, b.state])).toEqual([
      ['codex', 'out_of_quota'],
      ['openrouter', 'ready'],
      ['openai', 'not_set_up'],
      ['xai', 'not_set_up'],
    ]);
    expect(s.backends.find((b) => b.id === 'codex')?.hint).toBe('Usage limit reached on Codex.');
    expect(s.backends.find((b) => b.id === 'openrouter')?.hint).toMatch(/Not checked until OpenRouter is the active backend/);
  });

  it('never passes a file path through in a hint', () => {
    const s = deriveStatus({
      backend: 'codex',
      backends: {
        xai: { kind: 'api', ready: false, key_present: false, hint: 'Create a mode-600 file at ~/.config/os3-bridge/xai.key' },
        nous: { kind: 'rest', state: 'not_configured', hint: 'Nous key missing: /home/you/.config/os3-bridge/nous.key' },
        codex: { kind: 'cli', state: 'cli_missing', hint: 'codex not found in /opt/homebrew/bin or PATH.' },
      },
    });
    const hints = s.backends.map((b) => b.hint).join(' ');
    expect(hints).not.toMatch(/~\/|\/home\/|\/opt\//);
    expect(s.backends.find((b) => b.id === 'xai')?.hint).toBe('Set XAI_API_KEY, or save a key in Settings.');
    expect(s.backends.find((b) => b.id === 'nous')?.hint).toBe('Set NOUS_API_KEY, or save a key in Settings.');
  });

  it('treats a CLI that could not be started as missing', () => {
    const b = stateOf({ backend: 'x', backends: { codex: { kind: 'cli', cli_present: true, login: 'unchecked', hint: 'codex failed to start.' } } }, 'codex');
    expect(b?.state).toBe('cli_missing');
  });

  it('reports nothing set up when no backend is ready', () => {
    expect(nothingSetUp(deriveStatus(EMPTY_HEALTH))).toBe(true);
    expect(nothingSetUp(deriveStatus({}))).toBe(true);
  });
});

describe('envKeyWarning', () => {
  it('warns when an env var overrides a saved key file, and is quiet otherwise', () => {
    const status = deriveStatus({
      backend: 'openrouter',
      backends: {
        openrouter: { kind: 'api', ready: true, key_present: true, key_source: 'env', key_mode: '600' },
        openai: { kind: 'api', ready: true, key_present: true, key_source: 'file', key_mode: '600' },
      },
    });
    const [openrouter, openai] = status.backends;
    if (!openrouter || !openai) throw new Error('rows');
    expect(openrouter).toMatchObject({ keySource: 'env', keyFileSaved: true });
    expect(envKeyWarning(openrouter)).toMatch(/OPENROUTER_API_KEY is set .* saved OpenRouter key file is ignored/);
    expect(envKeyWarning(openai)).toBeNull();
  });
});

describe('failover', () => {
  const cooling = {
    backend: 'codex',
    backends: {
      codex: { kind: 'cli', state: 'out_of_quota', hint: 'Codex usage limit reached.' },
      'claude-code': { kind: 'cli', state: 'ready' },
    },
  };

  it('says requests fail over while the active backend cools down and a chain is on', () => {
    const on = deriveStatus({
      ...cooling,
      fallback: { enabled: true, chain: ['codex', 'claude-code'], active: 'claude-code', cooling: [{ backend: 'codex', reason: 'out_of_quota' }] },
    });
    expect(failoverNote(on)).toBe('Codex is cooling down after a usage limit. Requests fail over to Claude Code.');
    expect(servable(on).map((b) => b.id)).toEqual(['codex']);
  });

  it('stays quiet with no chain, so an exhausted backend has no runnable model', () => {
    const off = deriveStatus({ ...cooling, fallback: { enabled: false, chain: [] } });
    expect(failoverNote(off)).toBeNull();
    expect(servable(off)).toEqual([]);
  });
});
