import { act, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DoorStatus, NexusApi } from '../api/types';
import { AppProvider } from './AppState';

const STATUS: DoorStatus = { version: 't', activeBackend: 'codex', backends: [], uiApi: 1 };

describe('AppProvider polling', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('skips a 15s /health tick while the previous request is still in flight', async () => {
    vi.useFakeTimers();
    let finish: (s: DoorStatus) => void = () => undefined;
    const getStatus = vi.fn(
      () =>
        new Promise<DoorStatus>((resolve) => {
          finish = resolve;
        }),
    );
    const api: NexusApi = {
      mode: 'mock',
      getStatus,
      listModels: () => Promise.resolve([]),
      chat: () => Promise.reject(new Error('unused')),
      connect: () => Promise.reject(new Error('unused')),
      bench: () => Promise.resolve(null),
    };
    render(
      <AppProvider api={api}>
        <div />
      </AppProvider>,
    );
    expect(getStatus).toHaveBeenCalledTimes(1);
    act(() => {
      vi.advanceTimersByTime(45_000);
    });
    expect(getStatus).toHaveBeenCalledTimes(1);

    await act(async () => {
      finish(STATUS);
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(15_000);
    });
    expect(getStatus).toHaveBeenCalledTimes(2);
  });
});
