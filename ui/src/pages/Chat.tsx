import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { backendLabel, failoverNote } from '../api/status';
import ModelPicker from '../components/ModelPicker';
import { detailsLine } from '../components/format';
import { Elapsed, ErrorRow } from '../components/ui';
import { useApp } from '../state/context';

export default function Chat() {
  const { thread, send, stop, clear, waitingSince, selected, models, status } = useApp();
  const [input, setInput] = useState('');
  const bottomRef = useRef<HTMLDivElement>(null);
  const busy = waitingSince !== null;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [thread, busy]);

  const submit = () => {
    if (!input.trim() || busy || !selected) return;
    send(input);
    setInput('');
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const last = thread[thread.length - 1];
  const streamingText = busy && last?.kind === 'assistant';
  const note = failoverNote(status);

  return (
    <div className="flex flex-col h-[calc(100vh-8.5rem)] min-h-[480px] max-w-4xl mx-auto">
      <div className="flex flex-col sm:flex-row sm:items-center gap-3 mb-4">
        <ModelPicker disabled={busy} />
        <button type="button" className="btn btn-secondary text-sm" onClick={clear} disabled={busy || thread.length === 0}>
          Clear
        </button>
      </div>
      {note && models.length > 0 && (
        <p className="mb-3 text-sm text-amber-300" role="status">
          {note}
        </p>
      )}

      <div className="card flex-1 !p-0 overflow-hidden flex flex-col min-h-0">
        <div className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-4" aria-label="Thread">
          {thread.length === 0 && (
            <div className="h-full min-h-[200px] flex items-center justify-center text-center text-sm text-gray-500 px-4">
              {models.length === 0
                ? (note ?? `No model can run yet on ${backendLabel(status?.activeBackend ?? '')}. Open Status to see why.`)
                : 'Send a message. The reply streams from your backend as it is written.'}
            </div>
          )}
          {thread.map((item) => {
            if (item.kind === 'user') {
              return (
                <div key={item.id} className="flex justify-end">
                  <div className="max-w-[80%] rounded-2xl px-4 py-3 text-sm whitespace-pre-wrap bg-nexus-700 text-white">
                    {item.content}
                  </div>
                </div>
              );
            }
            if (item.kind === 'error') {
              return <ErrorRow key={item.id} message={item.message} hint={item.hint} />;
            }
            return (
              <div key={item.id} className="flex flex-col items-start gap-1">
                <div className="max-w-[80%] rounded-2xl px-4 py-3 text-sm whitespace-pre-wrap bg-gray-800 text-gray-100">
                  {item.content}
                </div>
                {item.result && (
                  <div className="text-xs text-gray-500 px-1" data-testid="reply-details">
                    {detailsLine(item.result)}
                  </div>
                )}
              </div>
            );
          })}
          {busy && !streamingText && (
            <div className="text-sm text-gray-400" aria-live="polite">
              Waiting for {selected?.model ?? 'the model'}… <Elapsed since={waitingSince} />
            </div>
          )}
          {busy && streamingText && (
            <div className="text-xs text-gray-500" aria-live="polite">
              Streaming… <Elapsed since={waitingSince} />
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        <div className="border-t border-gray-800 p-3 sm:p-4 flex gap-2 items-end">
          <textarea
            className="input flex-1 resize-none min-h-[44px] max-h-40"
            rows={1}
            placeholder={selected ? `Message ${selected.model}` : 'No model selected'}
            aria-label="Message"
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
            }}
            onKeyDown={onKey}
            disabled={!selected}
          />
          {busy ? (
            <button type="button" className="btn bg-red-600 hover:bg-red-500 text-white" onClick={stop}>
              Stop
            </button>
          ) : (
            <button type="button" className="btn btn-primary" onClick={submit} disabled={!input.trim() || !selected}>
              Send
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
