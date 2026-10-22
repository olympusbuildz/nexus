import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { json, mockDoor } from '../test/door';
import { ago, countdown } from './format';
import UsagePanel from './UsagePanel';

const BODY = {
  ok: true,
  threshold: 95,
  auto_switch: { enabled: true, serving: 'nous', benched: [{ backend: 'chatgpt' }] },
  backends: [
    {
      id: 'chatgpt',
      label: 'ChatGPT',
      active: true,
      source: 'real',
      plan: 'plus',
      level: 'critical',
      cooling: { backend: 'chatgpt', reason: 'usage_threshold' },
      windows: [
        { id: 'primary', label: '5h window', used_percent: 96, resets_in_s: 3720, level: 'critical' },
        { id: 'secondary', label: 'Weekly window', used_percent: 82, resets_in_s: 200000, level: 'warn' },
      ],
      from: 'usage endpoint',
      age_s: 30,
      estimate: { requests_1h: 3, tokens_1h: 1200, requests_5h: 9, tokens_5h: 4000, token_reports_1h: 2 },
    },
    {
      id: 'nous',
      label: 'Nous Portal',
      active: false,
      source: 'estimate',
      level: 'unknown',
      windows: [],
      estimate: { requests_1h: 1, tokens_1h: 50, requests_5h: 1, tokens_5h: 50 },
    },
  ],
};

describe('UsagePanel', () => {
  it('shows real bars with levels, countdowns, the estimate and the early switch', async () => {
    mockDoor({ 'GET /dashboard/usage': () => json(BODY) });
    render(<UsagePanel />);
    const panel = await screen.findByRole('region', { name: 'Usage' });
    const chatgpt = within(panel).getByRole('listitem', { name: 'ChatGPT usage' });
    const bars = within(chatgpt).getAllByRole('progressbar');
    expect(bars.map((b) => b.getAttribute('aria-valuenow'))).toEqual(['96', '82']);
    expect(bars.map((b) => b.parentElement?.getAttribute('data-level'))).toEqual(['critical', 'warn']);
    expect(bars.map((b) => b.firstElementChild?.className)).toEqual([
      expect.stringContaining('bg-red-500'),
      expect.stringContaining('bg-amber-400'),
    ]);
    expect(chatgpt.textContent).toMatch(/resets in 1h 2m/);
    expect(chatgpt.textContent).toMatch(/switched away early/);
    expect(chatgpt.textContent).toMatch(/From provider\s*usage endpoint · read just now/);
    expect(chatgpt.textContent).toMatch(/Estimate\s*requests counted by Nexus: 3 last hour, 9 last 5h/);
    expect(chatgpt.textContent).toMatch(/From provider\s*tokens the responses reported: 1200 last hour, 4000 last 5h/);
    expect(chatgpt.textContent).toMatch(/2 of 3 calls in the last hour reported usage/);
    expect(within(panel).getByRole('status').textContent).toMatch(/chatgpt at 95%: serving nous/);
    const nous = within(panel).getByRole('listitem', { name: 'Nous Portal usage' });
    expect(within(nous).queryByRole('progressbar')).toBeNull();
    expect(nous.textContent).toMatch(/Estimate.*not the provider.*1 requests \/ 50 tokens last hour/);
  });

  it('shows the image count and that it shares the plan percent', async () => {
    const [plan, ...rest] = BODY.backends;
    const withImages = {
      ...BODY,
      backends: [
        { ...plan, images_share_plan: true, estimate: { ...plan?.estimate, images_1h: 2, images_5h: 3, images_24h: 4 } },
        ...rest,
      ],
    };
    mockDoor({ 'GET /dashboard/usage': () => json(withImages) });
    render(<UsagePanel />);
    expect(await screen.findByText(/Images: 2 last hour, 3 last 5h, 4 last 24h/)).toBeTruthy();
    expect(screen.getByText(/shares the plan percent above with chat/)).toBeTruthy();
  });

  it('marks a reading whose window already reset, with its age', async () => {
    const nous = {
      id: 'nous',
      label: 'Nous Portal',
      active: false,
      source: 'real',
      level: 'unknown',
      from: 'response headers',
      age_s: 5 * 3600 + 120,
      windows: [{ id: 'requests', label: 'requests per minute', used_percent: 14, resets_in_s: 0, stale: true, level: 'unknown' }],
      estimate: {},
    };
    mockDoor({ 'GET /dashboard/usage': () => json({ ...BODY, backends: [nous] }) });
    render(<UsagePanel />);
    const row = await screen.findByRole('listitem', { name: 'Nous Portal usage' });
    expect(row.textContent).toMatch(/response headers · read 5h 2m ago/);
    expect(row.textContent).toMatch(/requests per minute \(reset since this reading, 5h 2m ago\)/);
    expect(within(row).getByRole('progressbar').parentElement?.getAttribute('data-level')).toBe('unknown');
  });

  it('shows a plan-endpoint note', async () => {
    const [plan] = BODY.backends;
    mockDoor({
      'GET /dashboard/usage': () => json({ ...BODY, backends: [{ ...plan, note: 'ChatGPT usage endpoint answered HTTP 403.' }] }),
    });
    render(<UsagePanel />);
    expect(await screen.findByText('ChatGPT usage endpoint answered HTTP 403.')).toBeTruthy();
  });

  it('formats reading ages', () => {
    expect(ago(null)).toBe('age unknown');
    expect(ago(5)).toBe('just now');
    expect(ago(125)).toBe('2m ago');
    expect(ago(3 * 3600 + 60)).toBe('3h 1m ago');
    expect(ago(90000)).toBe('1d 1h ago');
  });

  it('renders nothing on an older door without the route', async () => {
    const fetchMock = mockDoor({});
    const { container } = render(<UsagePanel />);
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).toHaveBeenCalled();
    expect(container.innerHTML).toBe('');
  });

  it('formats countdowns', () => {
    expect(countdown(null)).toBe('reset time not reported');
    expect(countdown(0)).toBe('resets now');
    expect(countdown(45)).toBe('resets in 45s');
    expect(countdown(125)).toBe('resets in 2m 5s');
    expect(countdown(90000)).toBe('resets in 1d 1h');
  });
});
