import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import App from './App';
import { createLiveApi } from './api/live';
import { BRIDGE_KEY, bodyOf, CODEX_MODELS, EMPTY_HEALTH, READY_HEALTH, chunk, json, mockDoor, sse, usage } from './test/door';

function readyDoor(chat: () => Response = () => sse([chunk('Hi '), chunk('there'), usage(5, 12)])) {
  return mockDoor({
    'GET /health': () => json(READY_HEALTH),
    'GET /dashboard/models': () => json(CODEX_MODELS),
    'GET /dashboard/bridge-key': () => json(BRIDGE_KEY),
    'POST /v1/chat/completions': chat,
  });
}

const nav = (name: string) => within(screen.getByRole('navigation', { name: 'Pages' })).getByRole('button', { name });

describe('App', () => {
  it('shows one connect card and no fake data when nothing is set up', async () => {
    mockDoor({ 'GET /health': () => json(EMPTY_HEALTH) });
    render(<App api={createLiveApi()} />);
    expect(await screen.findByRole('heading', { name: 'Connect one backend and test it' })).toBeInTheDocument();
    expect(screen.queryByText(/activity/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Backends' })).not.toBeInTheDocument();
  });

  it('lists one honest status per backend with a fix hint', async () => {
    readyDoor();
    render(<App api={createLiveApi()} />);
    const list = await screen.findByRole('list', { name: 'Backends' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringMatching(/Codex.*Ready/),
      expect.stringMatching(/Claude Code.*Not logged in.*claude/),
      expect.stringMatching(/GitHub Copilot.*CLI missing.*not found/),
      expect.stringMatching(/OpenRouter.*Not set up.*Set OPENROUTER_API_KEY, or save a key in Settings\./),
    ]);
  });

  it('keeps key file paths out of the Status rows', async () => {
    const path = '/Users/you/Library/Application Support/GrokBot/os3-setup/.openrouter-os3.key';
    mockDoor({
      'GET /health': () =>
        json({
          ...READY_HEALTH,
          backends: {
            ...READY_HEALTH.backends,
            openrouter: {
              name: 'OpenRouter',
              kind: 'rest',
              state: 'not_configured',
              ready: false,
              key_present: false,
              key_file: path,
              hint: `OpenRouter key missing. Set OPENROUTER_API_KEY or create a mode-600 file at ${path}`,
            },
          },
        }),
      'GET /dashboard/models': () => json(CODEX_MODELS),
    });
    const { container } = render(<App api={createLiveApi()} />);
    const list = await screen.findByRole('list', { name: 'Backends' });
    expect(within(list).getByText('Set OPENROUTER_API_KEY, or save a key in Settings.')).toBeInTheDocument();
    expect(container.innerHTML).not.toContain('Application Support');
    expect(container.innerHTML).not.toContain('/Users/');
  });

  it('Use in Chat selects that model, streams a reply with details, and keeps the thread across pages', async () => {
    const user = userEvent.setup();
    const fetchMock = readyDoor();
    render(<App api={createLiveApi()} />);
    await user.click(nav('Models'));
    expect(await screen.findByText('gpt-5.6-luna')).toBeInTheDocument();
    expect(screen.queryByText('catalog-only-model')).not.toBeInTheDocument();

    const luna = screen.getByText('gpt-5.6-luna').closest('li');
    if (!luna) throw new Error('no card');
    await user.click(within(luna).getByRole('button', { name: 'Use in Chat' }));

    const picker = screen.getByRole('combobox');
    expect(picker).toHaveDisplayValue('gpt-5.6-luna — Codex');
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hello{Enter}');

    expect(await screen.findByText('Hi there')).toBeInTheDocument();
    expect(screen.getByTestId('reply-details')).toHaveTextContent(/Codex · gpt-5.6-terra · \d+\.\ds · .*12 tokens · [\d.]+ tok\/s/);
    const body = bodyOf(fetchMock.mock.calls.find(([u]) => u === '/v1/chat/completions')?.[1]) as {
      model: string;
    };
    expect(body.model).toBe('gpt-5.6-luna');

    await user.click(nav('Bench'));
    await user.click(nav('Chat'));
    expect(screen.getByText('hello')).toBeInTheDocument();
    expect(screen.getByText('Hi there')).toBeInTheDocument();
    expect(screen.getByRole('combobox')).toHaveDisplayValue('gpt-5.6-luna — Codex');
  });

  it('shows an upstream error as a red row with its hint, never as assistant text', async () => {
    const user = userEvent.setup();
    readyDoor(() => json({ error: { message: 'Codex usage limit reached.', hint: 'Wait for the plan to reset.' } }, 429));
    render(<App api={createLiveApi()} />);
    await user.click(nav('Chat'));
    await waitFor(() => {
      expect(screen.getByRole('combobox')).not.toBeDisabled();
    });
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hello{Enter}');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Codex usage limit reached.');
    expect(alert).toHaveTextContent('Wait for the plan to reset.');
    expect(screen.queryByTestId('reply-details')).not.toBeInTheDocument();

    await user.click(nav('Status'));
    expect(await screen.findByText('Out of quota')).toBeInTheDocument();
  });

  it('shows a stream cut off before [DONE] as incomplete, never as a finished reply', async () => {
    const user = userEvent.setup();
    readyDoor(() => sse([chunk('Half a')], 200, false));
    render(<App api={createLiveApi()} />);
    await user.click(nav('Chat'));
    await waitFor(() => {
      expect(screen.getByRole('combobox')).not.toBeDisabled();
    });
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hello{Enter}');
    expect(await screen.findByRole('alert')).toHaveTextContent('cut off');
    expect(screen.getByText('Half a')).toBeInTheDocument();
    expect(screen.queryByTestId('reply-details')).not.toBeInTheDocument();
  });

  it('saves a key with activate:false on a ui_api 1 door, keeping the active backend', async () => {
    const user = userEvent.setup();
    const fetchMock = mockDoor({
      'GET /health': () => json({ ...EMPTY_HEALTH, ui_api: 1 }),
      'POST /dashboard/config': () =>
        json({ ok: true, backend: 'openrouter', activated: 'no', key_written: 'yes', model_note: 'Backend and model unchanged.' }),
    });
    render(<App api={createLiveApi()} />);
    await screen.findByRole('heading', { name: 'Connect one backend and test it' });
    await user.selectOptions(screen.getByRole('combobox'), 'openai');
    await user.type(screen.getByLabelText('OpenAI API key'), 'sk-test-not-real');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/OpenRouter is still the active backend/)).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(([u, init]) => u === '/dashboard/config' && init?.method === 'POST');
    expect(bodyOf(post?.[1])).toEqual({ backend: 'openai', api_key: 'sk-test-not-real', activate: false });
    expect(screen.getByLabelText('OpenAI API key')).toHaveValue('');
  });

  it('Settings save applies the backend without a restart notice', async () => {
    const user = userEvent.setup();
    const nousHealth = {
      ...READY_HEALTH,
      backend: 'nous',
      backends: {
        ...READY_HEALTH.backends,
        nous: { kind: 'api', ready: true, key_present: true, hint: 'ok' },
      },
    };
    let serving = 'nous';
    mockDoor({
      'GET /health': () => json(serving === 'nous' ? nousHealth : READY_HEALTH),
      'POST /dashboard/config': () => {
        serving = 'codex';
        return json({
          ok: true,
          backend: 'codex',
          activated: 'yes',
          restart: 'The door is serving this backend now.',
          restart_command: '',
        });
      },
    });
    render(<App api={createLiveApi()} />);
    await user.click(nav('Settings'));
    const before = await screen.findByRole('heading', { name: 'Serving now' });
    const beforeCard = before.closest('section');
    if (!beforeCard) throw new Error('no serving card');
    expect(within(beforeCard).getByText('Nous Portal')).toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox'), 'codex');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/The door is serving it now/)).toBeInTheDocument();
    expect(screen.queryByText(/restart/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Then press Test/)).not.toBeInTheDocument();
    const after = screen.getByRole('heading', { name: 'Serving now' }).closest('section');
    if (!after) throw new Error('no serving card');
    expect(within(after).getByText('Codex')).toBeInTheDocument();
  });

  it('shows a 409 reason and hint when Save is refused', async () => {
    const user = userEvent.setup();
    mockDoor({
      'GET /health': () => json({ ...READY_HEALTH, backend: 'nous' }),
      'POST /dashboard/config': () =>
        json(
          {
            ok: false,
            error: "Codex CLI isn't logged in.",
            code: 'not_ready',
            hint: 'Run codex login, then Save again.',
          },
          409,
        ),
    });
    render(<App api={createLiveApi()} />);
    await user.click(nav('Settings'));
    await user.selectOptions(await screen.findByRole('combobox'), 'codex');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Codex CLI isn't logged in.");
    expect(alert).toHaveTextContent('Run codex login, then Save again.');
    expect(screen.queryByText(/serving it now/)).not.toBeInTheDocument();
  });

  it('does not say the door is serving a backend /health does not mark ready', async () => {
    const user = userEvent.setup();
    let serving = 'nous';
    mockDoor({
      'GET /health': () =>
        json(
          serving === 'nous'
            ? {
                ...READY_HEALTH,
                backend: 'nous',
                backends: {
                  ...READY_HEALTH.backends,
                  nous: { kind: 'api', ready: true, key_present: true, hint: 'ok' },
                },
              }
            : {
                ...READY_HEALTH,
                backend: 'codex',
                ready: false,
                backends: {
                  ...READY_HEALTH.backends,
                  codex: {
                    kind: 'cli',
                    ready: false,
                    cli_present: true,
                    login: 'missing',
                    command: 'codex',
                    hint: 'CLI is not logged in. Run `codex login` in a terminal.',
                  },
                },
              },
        ),
      'POST /dashboard/config': () => {
        serving = 'codex';
        return json({ ok: true, backend: 'codex', activated: 'yes' });
      },
    });
    render(<App api={createLiveApi()} />);
    await user.click(nav('Settings'));
    await user.selectOptions(await screen.findByRole('combobox'), 'codex');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Codex is not ready.');
    expect(alert).toHaveTextContent('CLI is not logged in.');
    expect(screen.queryByText(/serving it now/)).not.toBeInTheDocument();
  });

  it('shows a Nous token warning next to a ready switch', async () => {
    const user = userEvent.setup();
    let serving = 'nous';
    mockDoor({
      'GET /health': () => json(serving === 'nous' ? { ...READY_HEALTH, backend: 'nous', backends: { ...READY_HEALTH.backends, nous: { kind: 'api', ready: true, key_present: true, hint: 'ok' } } } : READY_HEALTH),
      'POST /dashboard/config': () => {
        serving = 'codex';
        return json({
          ok: true,
          backend: 'codex',
          activated: 'yes',
          warning: 'Run `hermes auth add nous` first.',
        });
      },
    });
    render(<App api={createLiveApi()} />);
    await user.click(nav('Settings'));
    await user.selectOptions(await screen.findByRole('combobox'), 'codex');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/The door is serving it now/)).toBeInTheDocument();
    expect(screen.getByText('Run `hermes auth add nous` first.')).toBeInTheDocument();
  });

  it('sends no activate field to a door that predates ui_api', async () => {
    const user = userEvent.setup();
    const fetchMock = mockDoor({
      'GET /health': () => json(EMPTY_HEALTH),
      'POST /dashboard/config': () => json({ ok: true, backend: 'openai', restart: 'Restart to apply.' }),
    });
    render(<App api={createLiveApi()} />);
    await screen.findByRole('heading', { name: 'Connect one backend and test it' });
    await user.selectOptions(screen.getByRole('combobox'), 'openai');
    await user.type(screen.getByLabelText('OpenAI API key'), 'sk-test-not-real');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/Saved OpenAI/)).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(([u, init]) => u === '/dashboard/config' && init?.method === 'POST');
    expect(bodyOf(post?.[1])).toEqual({ backend: 'openai', api_key: 'sk-test-not-real' });
  });

  it('Save on the active backend with no new key posts nothing, so the saved model stays', async () => {
    const user = userEvent.setup();
    const fetchMock = readyDoor();
    render(<App api={createLiveApi()} />);
    await screen.findByRole('list', { name: 'Backends' });
    await user.click(nav('Settings'));
    expect(screen.getByRole('combobox')).toHaveValue('codex');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/Nothing changed: Codex is already the active backend/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u, init]) => u === '/dashboard/config' && init?.method === 'POST')).toBe(false);
  });

  it('Save on the active API backend with an empty key posts nothing', async () => {
    const user = userEvent.setup();
    const fetchMock = mockDoor({ 'GET /health': () => json(EMPTY_HEALTH) });
    render(<App api={createLiveApi()} />);
    await screen.findByRole('heading', { name: 'Connect one backend and test it' });
    expect(screen.getByRole('combobox')).toHaveValue('openrouter');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/Nothing changed: OpenRouter is already the active backend/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u, init]) => u === '/dashboard/config' && init?.method === 'POST')).toBe(false);
  });

  it('says the door fails over while the active backend cools down, and names the backend that answered', async () => {
    const user = userEvent.setup();
    mockDoor({
      'GET /health': () =>
        json({
          ...READY_HEALTH,
          backends: { ...READY_HEALTH.backends, codex: { ...READY_HEALTH.backends.codex, state: 'out_of_quota', hint: 'Codex usage limit reached.' } },
          fallback: { enabled: true, chain: ['codex', 'claude-code'], active: 'claude-code', cooling: [{ backend: 'codex', reason: 'out_of_quota' }] },
        }),
      'GET /dashboard/models': () => json(CODEX_MODELS),
      'GET /dashboard/bridge-key': () => json(BRIDGE_KEY),
      'POST /v1/chat/completions': () => sse([chunk('Hi'), usage(2, 1)], 200, true, { 'X-OS3-Backend': 'claude-code' }),
    });
    render(<App api={createLiveApi()} />);
    await user.click(nav('Chat'));
    expect(await screen.findByText('Codex is cooling down after a usage limit. Requests fail over to Claude Code.')).toBeInTheDocument();
    expect(screen.queryByText(/No model can run/)).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole('combobox')).not.toBeDisabled();
    });
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hello{Enter}');
    expect(await screen.findByTestId('reply-details')).toHaveTextContent(/^Claude Code \(failover from Codex\) · gpt-5.6-terra/);
  });

  it('Stop aborts a pending reply and shows the waiting timer while it waits', async () => {
    const user = userEvent.setup();
    let aborted = false;
    mockDoor({
      'GET /health': () => json(READY_HEALTH),
      'GET /dashboard/models': () => json(CODEX_MODELS),
      'GET /dashboard/bridge-key': () => json(BRIDGE_KEY),
      'POST /v1/chat/completions': (init) =>
        new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener('abort', () => {
            aborted = true;
            reject(new DOMException('Aborted', 'AbortError'));
          });
        }),
    });
    render(<App api={createLiveApi()} />);
    await user.click(nav('Chat'));
    await waitFor(() => {
      expect(screen.getByRole('combobox')).not.toBeDisabled();
    });
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hello{Enter}');
    expect(await screen.findByText(/Waiting for gpt-5.6-terra/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Stop' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Stopped.');
    expect(aborted).toBe(true);
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
  });

  it('Bench runs only the picked models with real token counts and explains failures', async () => {
    const user = userEvent.setup();
    const asked: string[] = [];
    mockDoor({
      'GET /health': () => json(READY_HEALTH),
      'GET /dashboard/models': () => json(CODEX_MODELS),
      'GET /dashboard/bridge-key': () => json(BRIDGE_KEY),
      'POST /v1/chat/completions': (init) => {
        const { model, max_tokens } = bodyOf(init) as { model: string; max_tokens?: number };
        asked.push(`${model}:${String(max_tokens)}`);
        return model === 'gpt-5.6-terra'
          ? sse([chunk('fine'), usage(4, 30)])
          : json({ error: { message: 'Model not in your plan.', hint: 'Pick another model.' } }, 400);
      },
    });
    render(<App api={createLiveApi()} />);
    await user.click(nav('Bench'));
    await user.click(await screen.findByRole('checkbox', { name: /gpt-5.6-terra/ }));
    await user.click(screen.getByRole('checkbox', { name: /gpt-5.6-luna/ }));
    await user.click(screen.getByRole('button', { name: 'Run 2 models' }));

    const results = await screen.findByRole('list', { name: 'Bench results' });
    await within(results).findByText('Done');
    await within(results).findByText('Failed');
    expect(asked).toEqual(['gpt-5.6-terra:64', 'gpt-5.6-luna:64']);
    expect(within(results).getByText('30')).toBeInTheDocument();
    expect(within(results).getByRole('alert')).toHaveTextContent('Model not in your plan.');
    expect(within(results).getByRole('alert')).toHaveTextContent('Pick another model.');
  });
});
