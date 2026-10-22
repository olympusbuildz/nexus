import { backendLabel, nothingSetUp, failoverNote } from '../api/status';
import ConnectCard from '../components/ConnectCard';
import { BackendTag, ErrorRow } from '../components/ui';
import { useApp } from '../state/context';

const SOURCE: Record<string, string> = {
  live: 'listed by the provider for this key',
  cli: 'reported by the CLI',
  default: 'the default /health checked',
};

export default function Models() {
  const { status, statusError, models, selected, select, navigate, loading } = useApp();

  if (loading && !status) return <div className="card animate-pulse h-40" aria-busy="true" />;
  if (statusError && !status) return <ErrorRow message={statusError.message} hint={statusError.hint} />;
  if (status && nothingSetUp(status)) return <ConnectCard />;

  const note = failoverNote(status);
  if (models.length === 0 && note) {
    return (
      <p className="card text-sm text-amber-300" role="status">
        {note}
      </p>
    );
  }
  if (models.length === 0) {
    const active = status?.backends.find((b) => b.active);
    return (
      <ErrorRow
        message={`No model can run on ${backendLabel(status?.activeBackend ?? '')} right now.`}
        hint={active?.hint ?? 'Check the Status page.'}
      />
    );
  }

  return (
    <div className="space-y-4">
      {note && (
        <p className="text-sm text-amber-300" role="status">
          {note}
        </p>
      )}
      <p className="text-sm text-gray-500">
        {models.length} model{models.length === 1 ? '' : 's'} that can run now. Each is tagged with the backend that
        serves it.
      </p>
      <ul className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {models.map((m) => {
          const isSelected = selected?.backend === m.backend && selected.model === m.id;
          return (
            <li key={`${m.backend}/${m.id}`} className={`card !p-4 flex flex-col gap-2 ${isSelected ? 'border-nexus-600/60' : ''}`}>
              <div className="flex items-start justify-between gap-2">
                <h3 className="font-mono text-sm break-all">{m.id}</h3>
                <BackendTag id={m.backend} />
              </div>
              <p className="text-xs text-gray-500">{SOURCE[m.source]}</p>
              <button
                type="button"
                className="mt-auto btn btn-secondary text-xs"
                onClick={() => {
                  select({ backend: m.backend, model: m.id });
                  navigate('chat');
                }}
              >
                Use in Chat
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
