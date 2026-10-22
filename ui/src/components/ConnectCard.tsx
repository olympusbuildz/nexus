import { useState, type FormEvent } from 'react';
import { BACKEND_LABELS, backendLabel, envKeyWarning, servable } from '../api/status';
import { isAbort, type ConnectResult } from '../api/types';
import { toApiError, useApp } from '../state/context';
import { ErrorRow } from './ui';

const CLI = new Set(['chatgpt', 'codex', 'claude-code', 'copilot', 'grok-build', 'antigravity']);

type Phase =
  | { kind: 'idle' }
  | { kind: 'busy'; step: string }
  | { kind: 'saved'; result: ConnectResult; keyFor: string }
  | { kind: 'applied'; backend: string; warning: string }
  | { kind: 'unchanged' }
  | { kind: 'tested'; text: string }
  | { kind: 'failed'; message: string; hint: string };

export default function ConnectCard({ title = 'Connect one backend and test it' }: { title?: string }) {
  const { api, status, refresh, loadModels } = useApp();
  const [backend, setBackend] = useState(status?.activeBackend || 'openrouter');
  const [apiKey, setApiKey] = useState('');
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' });
  const isCli = CLI.has(backend);
  const row = status?.backends.find((b) => b.id === backend);
  const envWarning = row ? envKeyWarning(row) : null;
  const keyOnly = (status?.uiApi ?? 0) >= 1;

  async function test() {
    setPhase({ kind: 'busy', step: 'Testing…' });
    const next = await refresh();
    const live = next ? servable(next).find((b) => b.id === backend) : undefined;
    if (!next || !live) {
      const now = next?.backends.find((b) => b.id === backend);
      setPhase({
        kind: 'failed',
        message: next?.activeBackend !== backend ? `The door is still serving ${backendLabel(next?.activeBackend ?? '')}.` : `${backendLabel(backend)} is not ready.`,
        hint: next?.activeBackend !== backend ? 'Save the new choice in Settings, then press Test again.' : now?.hint ?? '',
      });
      return;
    }
    const model = (await loadModels(backend))[0];
    if (!model) {
      setPhase({ kind: 'failed', message: 'No model this backend can run was found.', hint: live.hint });
      return;
    }
    try {
      const result = await api.chat({ backend, model: model.id, messages: [{ role: 'user', content: 'Reply with the word OK.' }] });
      setPhase({ kind: 'tested', text: `${model.id} answered in ${(result.totalMs / 1000).toFixed(1)}s: “${result.text.slice(0, 80)}”` });
    } catch (err) {
      if (isAbort(err)) return;
      const e = toApiError(err);
      setPhase({ kind: 'failed', message: e.message, hint: e.hint });
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const key = isCli ? '' : apiKey.trim();
    if (backend === status?.activeBackend && !key) {
      setApiKey('');
      setPhase({ kind: 'unchanged' });
      return;
    }
    setPhase({ kind: 'busy', step: 'Saving…' });
    try {
      const result = await api.connect({
        backend,
        apiKey: key || undefined,
        activate: key && keyOnly ? false : undefined,
      });
      setApiKey('');
      if (!result.activated) {
        setPhase({ kind: 'saved', result, keyFor: backend });
        return;
      }
      const next = await refresh();
      const row = next?.backends.find((b) => b.id === backend);
      const serving = next?.activeBackend === backend && row?.state === 'ready';
      if (serving) {
        setPhase({ kind: 'applied', backend, warning: result.warning ?? '' });
      } else if (result.warning || row) {
        setPhase({
          kind: 'failed',
          message:
            result.warning ||
            (next?.activeBackend === backend
              ? `${backendLabel(backend)} is not ready.`
              : `The door is still serving ${backendLabel(next?.activeBackend ?? '')}.`),
          hint: result.warning ? '' : row?.hint ?? '',
        });
      } else {
        setPhase({ kind: 'saved', result, keyFor: backend });
      }
    } catch (err) {
      setApiKey('');
      const x = toApiError(err);
      setPhase({ kind: 'failed', message: x.message, hint: x.hint });
    }
  }

  return (
    <section className="card max-w-xl" aria-labelledby="connect-title">
      <h2 id="connect-title" className="text-lg font-semibold mb-1">
        {title}
      </h2>
      <p className="text-sm text-gray-400 mb-5">
        Pick a plan CLI you are already signed in to, or paste one API key. The key goes straight to the door, which
        stores it mode 600. This page never keeps it.
      </p>
      <form className="space-y-4" onSubmit={(e) => void submit(e)}>
        <label className="block">
          <span className="block text-sm text-gray-400 mb-1">Backend</span>
          <select
            className="input w-full"
            value={backend}
            onChange={(e) => {
              setBackend(e.target.value);
              setPhase({ kind: 'idle' });
            }}
          >
            {Object.keys(BACKEND_LABELS).map((id) => (
              <option key={id} value={id}>
                {backendLabel(id)}
                {CLI.has(id) ? ' (plan CLI)' : ' (API key)'}
              </option>
            ))}
          </select>
          {row && row.state !== 'ready' && <span className="block mt-1 text-xs text-gray-500">{row.hint}</span>}
        </label>
        {!isCli && (
          <div>
          <label className="block">
            <span className="block text-sm text-gray-400 mb-1">{backendLabel(backend)} API key</span>
            <input
              className="input w-full"
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="Leave empty to keep the key already on file"
              value={apiKey}
              onChange={(e) => {
                setApiKey(e.target.value);
              }}
            />
          </label>
            {envWarning && <p className="mt-1 text-xs text-amber-300">{envWarning}</p>}
            {!keyOnly && (
              <p className="mt-1 text-xs text-gray-500">
                This door predates ui_api 1: saving a key also makes {backendLabel(backend)} the active backend.
              </p>
            )}
          </div>
        )}
        <div className="flex gap-2">
          <button type="submit" className="btn btn-primary" disabled={phase.kind === 'busy'}>
            Save
          </button>
          <button type="button" className="btn btn-secondary" disabled={phase.kind === 'busy'} onClick={() => void test()}>
            Test
          </button>
        </div>
      </form>
      <div className="mt-4 text-sm" aria-live="polite">
        {phase.kind === 'busy' && <p className="text-gray-400">{phase.step}</p>}
        {phase.kind === 'saved' && !phase.result.activated && (
          <p className="text-gray-300">
            Saved the {backendLabel(phase.keyFor)} key. {backendLabel(phase.result.backend)} is still the active backend
            and its saved model is unchanged.
          </p>
        )}
        {phase.kind === 'applied' && (
          <p className="text-gray-300">
            Saved {backendLabel(phase.backend)}. The door is serving it now.
            {phase.warning && <span className="mt-2 block text-amber-300">{phase.warning}</span>}
          </p>
        )}
        {phase.kind === 'saved' && phase.result.activated && (
          <p className="text-gray-300">
            Saved {backendLabel(phase.result.backend)}. {phase.result.restartNote}
            {phase.result.restartCommand && (
              <code className="block mt-2 font-mono text-xs bg-gray-800 rounded px-2 py-1">{phase.result.restartCommand}</code>
            )}
            <span className="block mt-2 text-gray-500">Then press Test.</span>
          </p>
        )}
        {phase.kind === 'unchanged' && (
          <p className="text-gray-300">
            Nothing changed: {backendLabel(backend)} is already the active backend, and its saved model is kept.
          </p>
        )}
        {phase.kind === 'tested' && <p className="text-emerald-300">{phase.text}</p>}
        {phase.kind === 'failed' && <ErrorRow message={phase.message} hint={phase.hint} />}
      </div>
    </section>
  );
}
