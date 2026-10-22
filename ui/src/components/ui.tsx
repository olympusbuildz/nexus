import { useEffect, useState } from 'react';
import { STATE_LABELS, backendLabel } from '../api/status';
import type { BackendState } from '../api/types';
import { seconds } from './format';

const PILL: Record<BackendState, string> = {
  ready: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  not_set_up: 'bg-gray-700/40 text-gray-300 border-gray-600',
  cli_missing: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  not_logged_in: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  out_of_quota: 'bg-red-500/15 text-red-300 border-red-500/30',
};

export function StatePill({ state }: { state: BackendState }) {
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium border ${PILL[state]}`}>
      {STATE_LABELS[state]}
    </span>
  );
}

export function NexusMark() {
  return (
    <svg viewBox="0 0 36 36" className="w-9 h-9 shrink-0" aria-hidden="true" data-testid="nexus-mark">
      <rect x="0.5" y="0.5" width="35" height="35" rx="9" fill="#0b0f14" stroke="#2ee6dc" strokeOpacity="0.3" />
      <path
        d="M15 9h-3a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h3"
        fill="none"
        stroke="#2ee6dc"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="drop-shadow-[0_0_4px_rgba(46,230,220,0.7)]"
      />
      <circle cx="22" cy="18" r="3" fill="#2ee6dc" />
    </svg>
  );
}

export function BackendTag({ id }: { id: string }) {
  return (
    <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[11px] font-medium bg-nexus-600/20 text-nexus-300 border border-nexus-600/30">
      {backendLabel(id)}
    </span>
  );
}

export function ErrorRow({ message, hint }: { message: string; hint: string }) {
  return (
    <div role="alert" className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm">
      <div className="font-medium text-red-300">{message}</div>
      {hint && <div className="mt-1 text-red-200/80">{hint}</div>}
    </div>
  );
}

export function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    const id = window.setInterval(() => {
      setNow(performance.now());
    }, 100);
    return () => {
      window.clearInterval(id);
    };
  }, []);
  return <span className="tabular-nums">{seconds(Math.max(0, now - since))}</span>;
}
