import { backendLabel } from '../api/status';
import type { ChatResult } from '../api/types';

export function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}

export function detailsLine(r: ChatResult): string {
  const who = r.fellBackFrom
    ? `${backendLabel(r.backend)} (failover from ${backendLabel(r.fellBackFrom)})`
    : backendLabel(r.backend);
  const parts = [who, r.model, seconds(r.totalMs)];
  if (r.firstTokenMs !== null) parts.push(`first token ${seconds(r.firstTokenMs)}`);
  parts.push(r.completionTokens !== null ? `${r.completionTokens} tokens` : 'tokens not reported');
  if (r.tokensPerSec !== null) parts.push(`${r.tokensPerSec.toFixed(1)} tok/s`);
  return parts.join(' · ');
}

export function countdown(seconds: number | null | undefined): string {
  if (seconds == null) return 'reset time not reported';
  if (seconds <= 0) return 'resets now';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (d) return `resets in ${d}d ${h}h`;
  if (h) return `resets in ${h}h ${m}m`;
  if (m) return `resets in ${m}m ${s}s`;
  return `resets in ${s}s`;
}

export function ago(seconds: number | null | undefined): string {
  if (seconds == null) return 'age unknown';
  if (seconds < 60) return 'just now';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h ago`;
  if (h) return `${h}h ${m}m ago`;
  return `${m}m ago`;
}
