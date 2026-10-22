import { backendLabel } from '../api/status';
import ConnectCard from '../components/ConnectCard';
import { StatePill } from '../components/ui';
import { useApp } from '../state/context';

export default function Settings() {
  const { status, api } = useApp();
  const active = status?.backends.find((b) => b.active);
  return (
    <div className="space-y-6 max-w-xl">
      <section className="card space-y-2">
        <h2 className="font-semibold">Serving now</h2>
        {active ? (
          <div className="flex items-center gap-3">
            <span>{backendLabel(active.id)}</span>
            <StatePill state={active.state} />
          </div>
        ) : (
          <p className="text-sm text-gray-400">No answer from the door yet.</p>
        )}
        {active && <p className="text-sm text-gray-400">{active.hint}</p>}
      </section>
      <ConnectCard title="Switch backend" />
      <section className="card text-sm text-gray-400 space-y-1">
        <h2 className="font-semibold text-gray-100">Keys</h2>
        <p>Provider keys are sent once to the door and written mode 600 on this machine. This page does not store them.</p>
        <p>
          The bridge key for <code className="font-mono">/v1</code> is read from the door when a chat starts and kept in
          memory for this tab only.
        </p>
        {api.mode === 'mock' && <p className="text-amber-300">Mock mode: nothing is sent to a door.</p>}
      </section>
    </div>
  );
}
