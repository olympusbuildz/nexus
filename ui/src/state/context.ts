import { createContext, useContext } from 'react';
import { ApiError, type ChatResult, type DoorStatus, type NexusApi, type RunnableModel } from '../api/types';

export type PageId = 'status' | 'models' | 'chat' | 'bench' | 'settings';

export interface Selection {
  backend: string;
  model: string;
}

export type ThreadItem =
  | { id: string; kind: 'user'; content: string }
  | { id: string; kind: 'assistant'; content: string; result: ChatResult | null }
  | { id: string; kind: 'error'; message: string; hint: string };

export interface AppState {
  api: NexusApi;
  page: PageId;
  navigate: (page: PageId) => void;
  status: DoorStatus | null;
  statusError: ApiError | null;
  loading: boolean;
  refresh: () => Promise<DoorStatus | null>;
  loadModels: (backend: string) => Promise<RunnableModel[]>;
  models: RunnableModel[];
  selected: Selection | null;
  select: (selection: Selection) => void;
  thread: ThreadItem[];
  waitingSince: number | null;
  send: (text: string) => void;
  stop: () => void;
  clear: () => void;
}

export const Ctx = createContext<AppState | null>(null);

export function useApp(): AppState {
  const value = useContext(Ctx);
  if (!value) throw new Error('useApp outside AppProvider');
  return value;
}

export function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err;
  return new ApiError(err instanceof Error ? err.message : 'Something failed.', {
    hint: 'Reload the page. ./doctor.sh shows whether the door is healthy.',
  });
}

