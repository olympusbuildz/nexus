import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { servable } from '../api/status';
import { Ctx, toApiError, type AppState, type PageId, type Selection, type ThreadItem } from './context';
import {
  ApiError,
  isAbort,
  type ChatResult,
  type ChatTurn,
  type DoorStatus,
  type NexusApi,
  type RunnableModel,
} from '../api/types';


let seq = 0;
const uid = () => `m${String(++seq)}`;

function withQuota(status: DoorStatus, quota: Record<string, string>): DoorStatus {
  if (!Object.keys(quota).length) return status;
  return {
    ...status,
    backends: status.backends.map((b) => {
      const hint = quota[b.id];
      return hint !== undefined && b.state === 'ready' ? { ...b, state: 'out_of_quota', hint } : b;
    }),
  };
}

const POLL_MS = 15_000;

export function AppProvider({ api, children }: { api: NexusApi; children: ReactNode }) {
  const [page, setPage] = useState<PageId>('status');
  const [rawStatus, setRawStatus] = useState<DoorStatus | null>(null);
  const [statusError, setStatusError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [modelsBy, setModelsBy] = useState<Record<string, RunnableModel[]>>({});
  const asked = useRef(new Set<string>());
  const [selected, setSelected] = useState<Selection | null>(null);
  const [thread, setThread] = useState<ThreadItem[]>([]);
  const [waitingSince, setWaitingSince] = useState<number | null>(null);
  const [quota, setQuota] = useState<Record<string, string>>({});
  const abortRef = useRef<AbortController | null>(null);
  const inFlight = useRef(0);

  const refresh = useCallback(async (): Promise<DoorStatus | null> => {
    inFlight.current += 1;
    try {
      const next = await api.getStatus();
      setRawStatus(next);
      setStatusError(null);
      return next;
    } catch (err) {
      setStatusError(toApiError(err));
      return null;
    } finally {
      inFlight.current -= 1;
      setLoading(false);
    }
  }, [api]);

  const loadModels = useCallback(
    async (backend: string): Promise<RunnableModel[]> => {
      asked.current.add(backend);
      let list: RunnableModel[] = [];
      try {
        list = await api.listModels(backend);
      } catch {
        list = [];
      }
      setModelsBy((m) => ({ ...m, [backend]: list }));
      return list;
    },
    [api],
  );

  const servableKey = rawStatus ? servable(rawStatus).map((b) => b.id).join(',') : '';
  useEffect(() => {
    for (const id of servableKey.split(',')) if (id && !asked.current.has(id)) void loadModels(id);
  }, [servableKey, loadModels]);
  const models = useMemo(() => servableKey.split(',').flatMap((id) => modelsBy[id] ?? []), [servableKey, modelsBy]);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => {
      if (inFlight.current === 0) void refresh();
    }, POLL_MS);
    return () => {
      window.clearInterval(id);
    };
  }, [refresh]);

  useEffect(() => {
    const still = selected && models.some((m) => m.id === selected.model && m.backend === selected.backend);
    if (still) return;
    const first = models[0];
    setSelected(first ? { backend: first.backend, model: first.id } : null);
  }, [models, selected]);

  const status = useMemo(() => (rawStatus ? withQuota(rawStatus, quota) : null), [rawStatus, quota]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const send = useCallback(
    (text: string) => {
      const prompt = text.trim();
      if (!prompt || abortRef.current || !selected) return;
      const history: ChatTurn[] = [];
      for (const item of thread) {
        if (item.kind === 'user') history.push({ role: 'user', content: item.content });
        if (item.kind === 'assistant' && item.result) history.push({ role: 'assistant', content: item.content });
      }
      history.push({ role: 'user', content: prompt });
      const replyId = uid();
      const controller = new AbortController();
      abortRef.current = controller;
      setWaitingSince(performance.now());
      setThread((prev) => [...prev, { id: uid(), kind: 'user', content: prompt }]);

      const patchReply = (content: string, result: ChatResult | null) => {
        setThread((prev) => {
          const at = prev.findIndex((m) => m.id === replyId);
          const item: ThreadItem = { id: replyId, kind: 'assistant', content, result };
          if (at === -1) return [...prev, item];
          const next = prev.slice();
          next[at] = item;
          return next;
        });
      };

      api
        .chat({
          backend: selected.backend,
          model: selected.model,
          messages: history,
          signal: controller.signal,
          onDelta: (partial) => {
            patchReply(partial, null);
          },
        })
        .then((result) => {
          patchReply(result.text, result);
          setQuota((q) =>
            result.backend in q ? Object.fromEntries(Object.entries(q).filter(([id]) => id !== result.backend)) : q,
          );
        })
        .catch((err: unknown) => {
          const aborted = isAbort(err);
          const e = aborted ? null : toApiError(err);
          setThread((prev) => [
            ...prev.filter((m) => m.id !== replyId || (m.kind === 'assistant' && m.content)),
            e
              ? { id: uid(), kind: 'error', message: e.message, hint: e.hint }
              : { id: uid(), kind: 'error', message: 'Stopped.', hint: 'The partial reply above is not sent back as history.' },
          ]);
          if (e?.status === 429) {
            setQuota((q) => ({ ...q, [selected.backend]: e.hint || e.message }));
          }
        })
        .finally(() => {
          abortRef.current = null;
          setWaitingSince(null);
        });
    },
    [api, selected, thread],
  );

  const clear = useCallback(() => {
    abortRef.current?.abort();
    setThread([]);
  }, []);

  const value = useMemo<AppState>(
    () => ({
      api,
      page,
      navigate: setPage,
      status,
      statusError,
      loading,
      refresh,
      loadModels,
      models,
      selected,
      select: setSelected,
      thread,
      waitingSince,
      send,
      stop,
      clear,
    }),
    [api, page, status, statusError, loading, refresh, loadModels, models, selected, thread, waitingSince, send, stop, clear],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
