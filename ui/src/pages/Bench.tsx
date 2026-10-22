import { useRef, useState } from 'react';
import { backendLabel, failoverNote } from '../api/status';
import { BENCH_MAX_TOKENS, isAbort, type BenchRow, type ChatResult, type RunnableModel } from '../api/types';
import { seconds } from '../components/format';
import { BackendTag, ErrorRow } from '../components/ui';
import { toApiError, useApp } from '../state/context';

type Row =
  | { key: string; model: RunnableModel; kind: 'queued' | 'running' }
  | { key: string; model: RunnableModel; kind: 'ok'; result: ChatResult }
  | { key: string; model: RunnableModel; kind: 'failed'; message: string; hint: string };

const keyOf = (m: RunnableModel) => `${m.backend}\n${m.id}`;
const DEFAULT_PROMPT = 'In two sentences, explain what a loopback address is.';
const MAX_TARGETS = 6;

function humanCode(code: string): string {
  const s = code.replace(/_/g, ' ');
  return `${s.charAt(0).toUpperCase()}${s.slice(1)}.`;
}

function fromDoor(m: RunnableModel, r: BenchRow | undefined): Row {
  const key = keyOf(m);
  if (!r) return { key, model: m, kind: 'failed', message: 'The door returned no row for this model.', hint: 'Run the bench again.' };
  if (!r.ok) return { key, model: m, kind: 'failed', message: humanCode(r.error?.code ?? 'failed'), hint: r.error?.hint ?? '' };
  return {
    key,
    model: m,
    kind: 'ok',
    result: {
      text: '',
      backend: r.backend || m.backend,
      model: r.model || m.id,
      firstTokenMs: null,
      totalMs: r.totalMs,
      promptTokens: r.inputTokens,
      completionTokens: r.outputTokens,
      tokensPerSec: r.tokensPerSec,
    },
  };
}

export default function Bench() {
  const { api, models, status } = useApp();
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);
  const [rows, setRows] = useState<Row[]>([]);
  const [running, setRunning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const toggle = (k: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  };

  const update = (row: Row) => {
    setRows((prev) => prev.map((r) => (r.key === row.key ? row : r)));
  };

  async function run() {
    const chosen = models.filter((m) => picked.has(keyOf(m)));
    if (!chosen.length || !prompt.trim()) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setRunning(true);
    setRows(chosen.map((m) => ({ key: keyOf(m), model: m, kind: 'queued' })));
    try {
      if (!(await runOnDoor(chosen, controller.signal))) await runHere(chosen, controller.signal);
    } finally {
      abortRef.current = null;
      setRunning(false);
    }
  }

  async function runOnDoor(chosen: RunnableModel[], signal: AbortSignal): Promise<boolean> {
    let out: BenchRow[] | null;
    try {
      out = await api.bench({ targets: chosen.map((m) => ({ backend: m.backend, model: m.id })), prompt, signal });
    } catch (err) {
      const e = isAbort(err) ? { message: 'Stopped.', hint: 'The bench was stopped.' } : toApiError(err);
      setRows(chosen.map((m) => ({ key: keyOf(m), model: m, kind: 'failed', message: e.message, hint: e.hint })));
      return true;
    }
    if (!out) return false;
    const rowsFromDoor = out;
    setRows(chosen.map((m, i) => fromDoor(m, rowsFromDoor[i])));
    return true;
  }

  async function runHere(chosen: RunnableModel[], signal: AbortSignal) {
    for (const m of chosen) {
      if (signal.aborted) {
        update({ key: keyOf(m), model: m, kind: 'failed', message: 'Not run.', hint: 'The bench was stopped.' });
        continue;
      }
      update({ key: keyOf(m), model: m, kind: 'running' });
      try {
        const result = await api.chat({ backend: m.backend, model: m.id, messages: [{ role: 'user', content: prompt }], maxTokens: BENCH_MAX_TOKENS, signal });
        update({ key: keyOf(m), model: m, kind: 'ok', result });
      } catch (err) {
        if (isAbort(err)) {
          update({ key: keyOf(m), model: m, kind: 'failed', message: 'Stopped.', hint: 'The bench was stopped mid-reply.' });
          continue;
        }
        const e = toApiError(err);
        update({ key: keyOf(m), model: m, kind: 'failed', message: e.message, hint: e.hint });
      }
    }
  }

  const note = failoverNote(status);
  if (models.length === 0 && note) {
    return (
      <p className="card text-sm text-amber-300" role="status">
        {note}
      </p>
    );
  }
  if (models.length === 0) {
    return (
      <ErrorRow
        message={`Nothing to bench: no model can run on ${backendLabel(status?.activeBackend ?? '')} right now.`}
        hint="Open Status to see what each backend needs."
      />
    );
  }

  const slowest = Math.max(1, ...rows.map((r) => (r.kind === 'ok' ? r.result.totalMs : 0)));

  return (
    <div className="space-y-5 max-w-4xl">
      <fieldset className="card space-y-3" disabled={running}>
        <legend className="sr-only">Models to bench</legend>
        <p className="text-sm text-gray-400">Pick up to {MAX_TARGETS} models. Each gets the same prompt, one at a time. This spends real quota.</p>
        <ul className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {models.map((m) => (
            <li key={keyOf(m)}>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={picked.has(keyOf(m))}
                  disabled={!picked.has(keyOf(m)) && picked.size >= MAX_TARGETS}
                  onChange={() => {
                    toggle(keyOf(m));
                  }}
                />
                <span className="font-mono break-all">{m.id}</span>
                <BackendTag id={m.backend} />
              </label>
            </li>
          ))}
        </ul>
        <label className="block">
          <span className="block text-sm text-gray-400 mb-1">Prompt</span>
          <textarea
            className="input w-full"
            rows={2}
            maxLength={2000}
            value={prompt}
            onChange={(e) => {
              setPrompt(e.target.value);
            }}
          />
        </label>
      </fieldset>
      <div className="flex gap-2">
        {running ? (
          <button type="button" className="btn bg-red-600 hover:bg-red-500 text-white" onClick={() => abortRef.current?.abort()}>
            Stop
          </button>
        ) : (
          <button type="button" className="btn btn-primary" disabled={picked.size === 0 || !prompt.trim()} onClick={() => void run()}>
            Run {picked.size || ''} model{picked.size === 1 ? '' : 's'}
          </button>
        )}
      </div>

      {rows.length > 0 && (
        <ul className="space-y-3" aria-label="Bench results">
          {rows.map((r) => (
            <li key={r.key} className="card !p-4 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="font-mono text-sm break-all">{r.model.id}</span>
                  <BackendTag id={r.model.backend} />
                </div>
                <span className="text-xs text-gray-400">
                  {r.kind === 'queued' && 'Pending'}
                  {r.kind === 'running' && 'Running…'}
                  {r.kind === 'ok' && <span className="text-emerald-300">Done</span>}
                  {r.kind === 'failed' && <span className="text-red-300">Failed</span>}
                </span>
              </div>
              {r.kind === 'ok' && (
                <>
                  <dl className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-sm">
                    <Stat label="Total" value={seconds(r.result.totalMs)} />
                    <Stat label="First token" value={r.result.firstTokenMs !== null ? seconds(r.result.firstTokenMs) : '—'} />
                    <Stat
                      label="Tokens"
                      value={r.result.completionTokens !== null ? String(r.result.completionTokens) : 'not reported'}
                    />
                    <Stat label="tok/s" value={r.result.tokensPerSec !== null ? r.result.tokensPerSec.toFixed(1) : '—'} />
                  </dl>
                  <div className="w-full bg-gray-800 rounded-full h-1.5">
                    <div className="h-1.5 rounded-full bg-nexus-500" style={{ width: `${String((r.result.totalMs / slowest) * 100)}%` }} />
                  </div>
                </>
              )}
              {r.kind === 'failed' && <ErrorRow message={r.message} hint={r.hint} />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-gray-500">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}
