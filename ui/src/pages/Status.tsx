import { envKeyWarning, nothingSetUp } from '../api/status';
import ConnectCard from '../components/ConnectCard';
import UsagePanel from '../components/UsagePanel';
import { ErrorRow, StatePill } from '../components/ui';
import { useApp } from '../state/context';

export default function Status() {
  const { status, statusError, loading } = useApp();

  if (loading && !status) return <div className="card animate-pulse h-40" aria-busy="true" />;
  if (statusError && !status) return <ErrorRow message={statusError.message} hint={statusError.hint} />;
  if (!status) return null;
  if (nothingSetUp(status)) return <ConnectCard />;

  return (
    <div className="space-y-4 max-w-3xl">
      {statusError && <ErrorRow message={statusError.message} hint={statusError.hint} />}
      <ul className="card !p-0 divide-y divide-gray-800" aria-label="Backends">
        {status.backends.map((b) => (
          <li key={b.id} className="px-5 py-4 flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-4">
            <div className="sm:w-44 shrink-0 flex items-center gap-2">
              <span className="font-medium">{b.label}</span>
              {b.active && <span className="text-[11px] uppercase tracking-wide text-nexus-300">active</span>}
            </div>
            <div className="sm:w-32 shrink-0">
              <StatePill state={b.state} />
            </div>
            <div className="min-w-0">
              <p className="text-sm text-gray-400">{b.hint}</p>
              {b.keyFileSaved && <p className="mt-1 text-xs text-amber-300">{envKeyWarning(b)}</p>}
            </div>
          </li>
        ))}
      </ul>
      <UsagePanel />
    </div>
  );
}
