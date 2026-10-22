import type { NexusApi } from './api/types';
import { useState } from 'react';
import Bench from './pages/Bench';
import Chat from './pages/Chat';
import Models from './pages/Models';
import Settings from './pages/Settings';
import Status from './pages/Status';
import { AppProvider } from './state/AppState';
import { useApp, type PageId } from './state/context';
import { backendLabel } from './api/status';
import { NexusMark } from './components/ui';

const NAV: { id: PageId; label: string }[] = [
  { id: 'status', label: 'Status' },
  { id: 'models', label: 'Models' },
  { id: 'chat', label: 'Chat' },
  { id: 'bench', label: 'Bench' },
  { id: 'settings', label: 'Settings' },
];

function Shell() {
  const { page, navigate, status, statusError, loading, api, waitingSince } = useApp();
  const [menuOpen, setMenuOpen] = useState(false);
  const active = status?.backends.find((b) => b.active);
  const doorWord = statusError && !status ? 'Door unreachable' : loading && !status ? 'Connecting' : active ? backendLabel(active.id) : '—';
  const dot = statusError && !status ? 'bg-red-500' : active?.state === 'ready' ? 'bg-emerald-500' : 'bg-amber-500';

  return (
    <div className="min-h-screen bg-gray-950 flex">
      {menuOpen && (
        <button
          type="button"
          aria-label="Close menu"
          className="fixed inset-0 bg-black/60 z-40 lg:hidden"
          onClick={() => {
            setMenuOpen(false);
          }}
        />
      )}
      <aside
        className={`fixed lg:sticky top-0 left-0 z-50 h-screen w-60 shrink-0 border-r border-gray-800 bg-gray-950 flex flex-col transition-transform lg:translate-x-0 ${
          menuOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="px-5 py-5 flex items-center gap-3 border-b border-gray-800">
          <NexusMark />
          <div>
            <div className="font-extrabold tracking-tight">
              Nex<span className="text-nexus-400">us</span>
            </div>
            <div className="text-xs text-gray-500">Your AI plans, one endpoint</div>
          </div>
        </div>
        <nav className="flex-1 p-3 space-y-1" aria-label="Pages">
          {NAV.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-current={page === item.id ? 'page' : undefined}
              onClick={() => {
                navigate(item.id);
                setMenuOpen(false);
              }}
              className={`w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                page === item.id ? 'bg-nexus-400/10 text-nexus-300 border border-nexus-400/40' : 'text-gray-400 hover:text-gray-100 hover:bg-gray-900'
              }`}
            >
              {item.label}
              {item.id === 'chat' && waitingSince !== null && <span className="w-1.5 h-1.5 rounded-full bg-nexus-400 animate-pulse" />}
            </button>
          ))}
        </nav>
        <div className="p-4 border-t border-gray-800 text-xs text-gray-500 flex items-center justify-between">
          <span>{status && /^\d/.test(status.version) ? `v${status.version}` : status?.version}</span>
          <span className="flex items-center gap-1.5">
            <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />
            {doorWord}
          </span>
        </div>
      </aside>

      <div className="flex-1 flex flex-col min-w-0">
        <header className="sticky top-0 z-30 border-b border-gray-800 bg-gray-950/80 backdrop-blur px-4 sm:px-6 py-3.5 flex items-center gap-3">
          <button
            type="button"
            className="lg:hidden btn btn-secondary !px-2.5 !py-1.5 text-sm"
            aria-label="Open menu"
            onClick={() => {
              setMenuOpen(true);
            }}
          >
            Menu
          </button>
          <h1 className="text-lg font-semibold tracking-tight">{NAV.find((n) => n.id === page)?.label}</h1>
          {api.mode === 'mock' && (
            <span className="ml-auto text-xs px-2 py-0.5 rounded border border-amber-500/40 text-amber-300 bg-amber-500/10">
              Mock data — not your door
            </span>
          )}
        </header>
        <main className="flex-1 p-4 sm:p-6 lg:p-8 overflow-auto">
          {page === 'status' && <Status />}
          {page === 'models' && <Models />}
          {page === 'chat' && <Chat />}
          {page === 'bench' && <Bench />}
          {page === 'settings' && <Settings />}
        </main>
      </div>
    </div>
  );
}

export default function App({ api }: { api: NexusApi }) {
  return (
    <AppProvider api={api}>
      <Shell />
    </AppProvider>
  );
}
