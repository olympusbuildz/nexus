import { useEffect, useState } from 'react';
import { ago, countdown } from './format';

export interface UsageWindow {
  id: string;
  label?: string;
  used_percent: number;
  window_minutes?: number | null;
  resets_in_s?: number | null;
  stale?: boolean;
  limit?: number | null;
  remaining?: number | null;
  level: Level;
}
type Level = 'ok' | 'warn' | 'critical' | 'unknown';
export interface UsageRow {
  id: string;
  label: string;
  active: boolean;
  source: 'real' | 'estimate';
  from?: string | null;
  age_s?: number | null;
  note?: string;
  windows: UsageWindow[];
  estimate: Record<string, number>;
  level: Level;
  plan?: string;
  images_share_plan?: boolean;
  account?: { used_percent?: number; usage?: number; limit?: number; note?: string };
  cooling?: { reason: string; seconds_left?: number; until?: string };
}
export interface UsageBody {
  threshold: number | null;
  auto_switch: { enabled: boolean; serving?: string | null; benched: { backend: string }[] };
  backends: UsageRow[];
}

const BAR: Record<Level, string> = {
  ok: 'bg-emerald-500',
  warn: 'bg-amber-400',
  critical: 'bg-red-500',
  unknown: 'bg-gray-500',
};
const TEXT: Record<Level, string> = {
  ok: 'text-gray-300',
  warn: 'text-amber-300',
  critical: 'text-red-400',
  unknown: 'text-gray-400',
};

function Tag({ children, tone }: { children: string; tone: 'real' | 'estimate' }) {
  const color = tone === 'real' ? 'text-emerald-300' : 'text-gray-400';
  return <span className={`uppercase tracking-wide mr-1 ${color}`}>{children}</span>;
}

function Bar({ label, percent, level, note }: { label: string; percent: number; level: Level; note: string }) {
  const pct = Math.max(0, Math.min(100, percent));
  return (
    <div className="space-y-1" data-level={level}>
      <div className="flex justify-between text-xs">
        <span className="text-gray-400">{label}</span>
        <span className={TEXT[level]}>
          {Math.round(percent)}% used · {note}
        </span>
      </div>
      <div
        className="h-2 rounded bg-gray-800 overflow-hidden"
        role="progressbar"
        aria-label={label}
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className={`h-full ${BAR[level]}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function Row({ row, elapsed }: { row: UsageRow; elapsed: number }) {
  const e = row.estimate;
  const age = row.age_s == null ? null : row.age_s + elapsed;
  const reports = e.token_reports_1h;
  return (
    <li className="px-5 py-4 space-y-3" aria-label={`${row.label} usage`}>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-medium">{row.label}</span>
        {row.active && <span className="text-[11px] uppercase tracking-wide text-nexus-300">active</span>}
        {row.plan && <span className="text-[11px] uppercase tracking-wide text-gray-500">{row.plan}</span>}
        {row.cooling?.reason === 'usage_threshold' && (
          <span className="text-xs text-red-300">switched away early: near its limit</span>
        )}
        {row.cooling && row.cooling.reason !== 'usage_threshold' && (
          <span className="text-xs text-amber-300">cooling down ({row.cooling.reason})</span>
        )}
      </div>
      {row.windows.length > 0 && (
        <p className="text-xs text-gray-500">
          <Tag tone="real">From provider</Tag>
          {row.from ?? 'response headers'} · read {ago(age)}
        </p>
      )}
      {row.note && <p className="text-xs text-amber-300">{row.note}</p>}
      {row.windows.map((w) => (
        <Bar
          key={w.id}
          label={`${w.label ?? w.id}${w.stale ? ` (reset since this reading, ${ago(age)})` : ''}`}
          percent={w.used_percent}
          level={w.stale ? 'unknown' : w.level}
          note={countdown(w.resets_in_s == null ? null : w.resets_in_s - elapsed)}
        />
      ))}
      {row.account?.used_percent != null && (
        <Bar
          label="Key credit"
          percent={row.account.used_percent}
          level={row.level}
          note={`$${row.account.usage ?? 0} of $${row.account.limit ?? '?'}`}
        />
      )}
      {row.account?.note && <p className="text-xs text-amber-300">{row.account.note}</p>}
      {reports === undefined ? (
        <p className="text-xs text-gray-500">
          <Tag tone="estimate">Estimate</Tag>
          counted by Nexus, not the provider: {e.requests_1h ?? 0} requests / {e.tokens_1h ?? 0} tokens last hour,{' '}
          {e.requests_5h ?? 0} / {e.tokens_5h ?? 0} last 5h
        </p>
      ) : (
        <>
          <p className="text-xs text-gray-500">
            <Tag tone="estimate">Estimate</Tag>
            requests counted by Nexus: {e.requests_1h ?? 0} last hour, {e.requests_5h ?? 0} last 5h
          </p>
          <p className="text-xs text-gray-500">
            <Tag tone="real">From provider</Tag>
            tokens the responses reported: {e.tokens_1h ?? 0} last hour, {e.tokens_5h ?? 0} last 5h
            {` (${reports} of ${e.requests_1h ?? 0} calls in the last hour reported usage)`}
          </p>
        </>
      )}
      {e.images_24h !== undefined && (
        <p className="text-xs text-gray-500">
          Images: {e.images_1h ?? 0} last hour, {e.images_5h ?? 0} last 5h, {e.images_24h} last 24h
          {row.images_share_plan ? ' (shares the plan percent above with chat)' : ''}
        </p>
      )}
    </li>
  );
}

export default function UsagePanel({ intervalMs = 30000 }: { intervalMs?: number }) {
  const [body, setBody] = useState<UsageBody | null>(null);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const res = await fetch('/dashboard/usage', { cache: 'no-store' });
        if (!res.ok) throw new Error(String(res.status));
        const next = (await res.json()) as UsageBody | null;
        if (live && Array.isArray(next?.backends)) {
          setBody(next);
          setElapsed(0);
        }
      } catch {
        if (live) setBody(null);
      }
    };
    void load();
    const poll = setInterval(() => {
      void load();
    }, intervalMs);
    const tick = setInterval(() => {
      setElapsed((s) => s + 1);
    }, 1000);
    return () => {
      live = false;
      clearInterval(poll);
      clearInterval(tick);
    };
  }, [intervalMs]);

  if (!body || body.backends.length === 0) return null;
  const benched = body.auto_switch.benched.map((b) => b.backend);
  return (
    <section className="card !p-0" aria-label="Usage">
      <header className="px-5 pt-4 pb-2 flex items-baseline justify-between gap-2 flex-wrap">
        <h2 className="font-medium">Usage and resets</h2>
        <span className="text-xs text-gray-500">
          {body.threshold == null || !body.auto_switch.enabled
            ? 'auto-switch off'
            : `auto-switch at ${body.threshold}%`}
        </span>
      </header>
      {benched.length > 0 && (
        <p className="px-5 pb-2 text-sm text-red-300" role="status">
          {benched.join(', ')} at {body.threshold}%: serving {body.auto_switch.serving ?? 'the next backend'} until it
          resets.
        </p>
      )}
      <ul className="divide-y divide-gray-800">
        {body.backends.map((row) => (
          <Row key={row.id} row={row} elapsed={elapsed} />
        ))}
      </ul>
    </section>
  );
}
