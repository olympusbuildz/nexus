"""Same-origin dashboard page. No credentials in the HTML, no second listener.

The page is static. Live status comes from GET /health, GET /doctor,
GET /dashboard/pulse, and, when the model picker needs it, GET /dashboard/models.
The bridge key is fetched only when Copy key is clicked, and it is never written
into the document. This module never reads a key file.
Faces are self-hosted from /dashboard/fonts so the page does not phone home.

One status (Ready, Unconfirmed, Not ready, Down) is computed once in the page
from /health and feeds the status strip, the failure banner, and the Configure
summary, so they cannot tell different stories.
"""

from __future__ import annotations

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexus</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23070a0f'/%3E%3Cpath d='M18 48V16l28 32V16' fill='none' stroke='%232ee6dc' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
  @font-face {
    font-family: "Syne Door";
    src: url("/dashboard/fonts/syne.woff2") format("woff2");
    font-weight: 400 800;
    font-display: swap;
  }
  @font-face {
    font-family: "Plex Door";
    src: url("/dashboard/fonts/plex-sans.woff2") format("woff2");
    font-weight: 100 700;
    font-display: swap;
  }
  @font-face {
    font-family: "Plex Mono Door";
    src: url("/dashboard/fonts/plex-mono-400.woff2") format("woff2");
    font-weight: 400;
    font-display: swap;
  }
  @font-face {
    font-family: "Plex Mono Door";
    src: url("/dashboard/fonts/plex-mono-500.woff2") format("woff2");
    font-weight: 500;
    font-display: swap;
  }
  :root {
    color-scheme: dark;
    --bg: #070a0f;
    --ink: #eaf0f4;
    --muted: #9aa6b4;
    --line: rgba(214, 232, 236, 0.12);
    --glass: #0e141b;
    --inset: #090d12;
    --accent: #2ee6dc;
    --accent-ink: #041716;
    --ok: #3ddc8a;
    --warn: #e2a04a;
    --bad: #ff5d6c;
    --state: var(--accent);
    --shadow: 0 10px 30px rgba(0, 0, 0, 0.28);
    --radius: 12px;
    --radius-sm: 8px;
    --gap: 28px;
    --pad: 28px;
    --display: "Syne Door", "Avenir Next", "Segoe UI", sans-serif;
    --sans: "Plex Door", "Segoe UI", system-ui, sans-serif;
    --mono: "Plex Mono Door", ui-monospace, Menlo, Consolas, monospace;
  }
  body[data-level="warn"] { --state: var(--warn); }
  body[data-level="bad"] { --state: var(--bad); }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body {
    margin: 0;
    min-height: 100vh;
    font-family: var(--sans);
    font-size: 15px;
    line-height: 1.6;
    letter-spacing: 0.005em;
    color: var(--ink);
    background-color: var(--bg);
    background-image:
      linear-gradient(rgba(255, 255, 255, 0.018) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255, 255, 255, 0.018) 1px, transparent 1px);
    background-size: 56px 56px;
    -webkit-font-smoothing: antialiased;
  }
  .wash {
    position: fixed;
    inset: 0;
    z-index: 0;
    pointer-events: none;
    background:
      linear-gradient(90deg, rgba(46, 230, 220, 0.08), transparent 22%),
      radial-gradient(90% 70% at 50% 120%, rgba(0, 0, 0, 0.5), transparent 60%);
  }
  @keyframes breathe {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(0.82); opacity: 0.7; }
  }
  @keyframes enter {
    from { opacity: 0; transform: translateY(-6px); }
    to { opacity: 1; transform: none; }
  }
  @keyframes sweep { to { transform: rotate(360deg); } }
  @keyframes ripple {
    from { transform: scale(1); opacity: 0.7; }
    to { transform: scale(3.2); opacity: 0; }
  }
  @keyframes ping {
    from { transform: scale(1); opacity: 0.9; }
    to { transform: scale(4.5); opacity: 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation: none !important;
      transition: none !important;
    }
  }
  main {
    position: relative;
    z-index: 1;
    max-width: 1080px;
    margin: 0 auto;
    padding: 56px 32px 120px;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 24px;
    margin-bottom: 40px;
  }
  .brand { display: flex; gap: 18px; align-items: center; }
  .mark {
    width: 24px;
    height: 56px;
    flex: none;
    border: 1px solid rgba(46, 230, 220, 0.6);
    border-right: 0;
    border-radius: 6px 0 0 6px;
    background: #070a0f;
    box-shadow: inset -8px 0 16px rgba(46, 230, 220, 0.18);
    position: relative;
  }
  .mark span {
    position: absolute;
    right: 0;
    top: 8px;
    bottom: 8px;
    width: 2px;
    background: var(--accent);
    box-shadow: 0 0 10px var(--accent);
  }
  h1 {
    font-family: var(--display);
    font-size: 44px;
    font-weight: 800;
    letter-spacing: -0.04em;
    margin: 4px 0 0;
    line-height: 1;
  }
  h1 span {
    color: var(--accent);
    font-weight: 700;
  }
  h2 {
    font-family: var(--sans);
    font-size: 18px;
    font-weight: 650;
    letter-spacing: -0.005em;
    line-height: 1.3;
    color: var(--ink);
    margin: 0;
  }
  h3 {
    font-family: var(--sans);
    font-size: 15px;
    margin: 0 0 6px;
    font-weight: 650;
    letter-spacing: 0;
    line-height: 1.3;
  }
  p { margin: 0; color: var(--muted); line-height: 1.6; }
  .kicker {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.06em;
    color: var(--accent);
  }
  #subtitle {
    margin-top: 8px;
    font-size: 13px;
  }
  .bind {
    margin: 0;
    padding: 8px 14px;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: var(--glass);
    color: var(--ink);
    font-size: 13px;
  }
  .banner {
    position: relative;
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 16px;
    align-items: start;
    margin-bottom: var(--gap);
    padding: 20px 24px 20px 28px;
    border: 1px solid var(--bad);
    border-radius: var(--radius);
    overflow: hidden;
    background: rgba(255, 93, 108, 0.1);
    box-shadow: var(--shadow);
  }
  .banner::before {
    content: "";
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 4px;
    background: var(--bad);
  }
  .banner.warn { border-color: var(--warn); background: rgba(226, 160, 74, 0.1); }
  .banner.warn::before { background: var(--warn); }
  .banner-glyph {
    width: 28px;
    height: 28px;
    display: grid;
    place-items: center;
    border-radius: 50%;
    font-weight: 800;
    color: #070a0f;
    background: var(--bad);
  }
  .banner.warn .banner-glyph { background: var(--warn); }
  .banner-title {
    font-size: 17px;
    font-weight: 650;
    line-height: 1.35;
    color: var(--ink);
  }
  .banner-detail { color: var(--ink); font-size: 14px; margin-top: 6px; overflow-wrap: anywhere; }
  .banner-extra { color: var(--muted); font-size: 13px; margin-top: 8px; }
  .hero, .card {
    background: var(--glass);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: var(--gap);
    min-width: 0;
  }
  .hero { position: relative; overflow: hidden; padding: 24px 0 8px; }
  .hero::before {
    content: "";
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 3px;
    background: var(--state);
  }
  .card { padding: var(--pad); }
  .hero-top, .card-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
  }
  .hero-top { padding: 0 var(--pad); }
  .card-head { margin-bottom: 18px; }
  .live {
    font-size: 13px;
    color: var(--muted);
  }
  .grid {
    display: grid;
    grid-template-columns: 1.35fr 0.8fr;
    gap: var(--gap);
    margin-bottom: var(--gap);
  }
  .grid .card { margin-bottom: 0; }
  .stats {
    display: grid;
    grid-template-columns: 0.8fr 0.9fr 1.5fr 0.9fr;
    margin-top: 16px;
  }
  .stat {
    padding: 4px 24px 20px var(--pad);
    border-right: 1px solid var(--line);
    min-width: 0;
  }
  .stat + .stat { padding-left: 24px; }
  .stat:last-child { border-right: 0; }
  .stat > span, label, th, dt {
    color: var(--muted);
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.03em;
  }
  .stat strong {
    display: flex;
    align-items: center;
    gap: 12px;
    font-family: var(--sans);
    font-size: 19px;
    font-weight: 650;
    letter-spacing: -0.01em;
    line-height: 1.3;
    margin-top: 10px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .stat.service strong { font-size: 28px; line-height: 1.15; }
  .stat.model strong {
    font-family: var(--mono);
    font-size: 14px;
    font-weight: 500;
    letter-spacing: 0;
    line-height: 1.45;
  }
  .stat small {
    display: block;
    margin-top: 8px;
    color: var(--muted);
    font-size: 13px;
  }
  .stale { opacity: 0.55; }
  .orb {
    position: relative;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    flex: none;
    background: var(--muted);
    box-shadow: 0 0 0 5px rgba(154, 166, 180, 0.14);
  }
  .orb::after {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: 50%;
    border: 1px solid currentColor;
    opacity: 0;
  }
  .orb.ok {
    background: var(--ok);
    color: var(--ok);
    box-shadow: 0 0 0 6px rgba(61, 220, 138, 0.14);
    animation: breathe 2.8s ease-in-out infinite;
  }
  .orb.ok::after { animation: ripple 2.8s ease-out infinite; }
  .orb.warn { background: var(--warn); box-shadow: 0 0 0 6px rgba(226, 160, 74, 0.16); }
  .orb.bad { background: var(--bad); box-shadow: 0 0 0 6px rgba(255, 93, 108, 0.16); }
  .ok { color: var(--ok); }
  .warn { color: var(--warn); }
  .bad { color: var(--bad); }
  .note { font-size: 13px; line-height: 1.6; margin-top: 14px; }
  .hero .note { padding: 0 var(--pad) 16px; margin-top: 0; }
  .mono, code, .model-id, .route, #model, #model-pick {
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
  }
  code { font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
  .well {
    position: relative;
    overflow: hidden;
    background: #04070a;
    color: #dcf3f1;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    min-height: 248px;
    padding: 16px;
    display: grid;
    grid-template-columns: 196px minmax(0, 1fr);
    column-gap: 20px;
    align-items: start;
  }
  .well .ok { color: #3ddc8a; }
  .well .bad { color: #ff6b78; }
  .scope {
    position: relative;
    width: 180px;
    height: 180px;
    margin: 8px 8px 12px;
    border-radius: 50%;
    border: 1px solid rgba(46, 230, 220, 0.4);
    background:
      radial-gradient(circle, transparent 0 24%, rgba(46, 230, 220, 0.16) 24.6%, transparent 25.4%),
      radial-gradient(circle, transparent 0 49%, rgba(46, 230, 220, 0.16) 49.6%, transparent 50.4%),
      radial-gradient(circle, transparent 0 74%, rgba(46, 230, 220, 0.16) 74.6%, transparent 75.4%),
      linear-gradient(rgba(46, 230, 220, 0.12), rgba(46, 230, 220, 0.12)) center / 1px 100% no-repeat,
      linear-gradient(90deg, rgba(46, 230, 220, 0.12), rgba(46, 230, 220, 0.12)) center / 100% 1px no-repeat,
      #04090b;
    box-shadow: inset 0 0 24px rgba(46, 230, 220, 0.08);
  }
  .scope::before {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: 50%;
    background: conic-gradient(from 0deg, transparent 0 74%, rgba(46, 230, 220, 0.4) 100%);
    animation: sweep 4.8s linear infinite;
  }
  .scope::after {
    content: "";
    position: absolute;
    left: 50%;
    top: 50%;
    width: 6px;
    height: 6px;
    margin: -3px 0 0 -3px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 10px var(--accent);
  }
  .scope.down { border-color: rgba(255, 93, 108, 0.55); }
  .scope.down::before { animation-play-state: paused; background: none; }
  .scope.down::after { background: var(--bad); box-shadow: 0 0 12px var(--bad); }
  .blip {
    position: absolute;
    width: 7px;
    height: 7px;
    margin: -3.5px 0 0 -3.5px;
    border-radius: 50%;
    background: #2ee6dc;
    box-shadow: 0 0 8px #2ee6dc;
  }
  .blip.bad { background: #ff5d6c; box-shadow: 0 0 9px #ff5d6c; }
  .blip.fresh::after {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: 50%;
    border: 1px solid currentColor;
    color: inherit;
    animation: ping 1.6s ease-out 3;
  }
  .scope-read {
    grid-column: 1;
    margin: 0 8px;
    font-size: 13px;
    line-height: 1.5;
    color: rgba(220, 243, 241, 0.72);
  }
  .well-body { grid-column: 2; grid-row: 1 / span 2; min-width: 0; }
  .quiet { padding: 56px 16px 40px; text-align: center; position: relative; }
  .quiet-title {
    color: #f2fffd;
    font-size: 16px;
    font-weight: 650;
    margin-bottom: 8px;
  }
  .quiet p { color: rgba(220, 243, 241, 0.72); font-size: 14px; }
  .traffic {
    margin: 0 0 14px;
    padding: 12px 14px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: rgba(7, 14, 18, 0.55);
  }
  .traffic-title {
    font-size: 13px;
    font-weight: 650;
    color: #f2fffd;
    margin: 0 0 8px;
  }
  .traffic-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px 14px;
    margin: 0 0 8px;
  }
  .traffic-grid div span {
    display: block;
    font-size: 11px;
    letter-spacing: 0.03em;
    color: rgba(220, 243, 241, 0.55);
    margin-bottom: 2px;
  }
  .traffic-grid div strong {
    font-size: 15px;
    font-weight: 650;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
  }
  .traffic-grid div strong.bad { color: var(--bad); }
  .traffic-note {
    margin: 0;
    font-size: 12.5px;
    line-height: 1.45;
    color: rgba(220, 243, 241, 0.62);
  }
  @media (max-width: 720px) {
    .traffic-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  .feed { list-style: none; margin: 0; padding: 0; position: relative; font-variant-numeric: tabular-nums; }
  .feed-head, .hit {
    display: grid;
    grid-template-columns: 88px minmax(0, 1.4fr) minmax(0, 1.1fr) 52px 72px;
    gap: 12px;
    align-items: baseline;
    padding: 10px 12px;
  }
  .feed-head {
    color: rgba(220, 243, 241, 0.6);
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.03em;
  }
  .hit {
    font-size: 13px;
    line-height: 1.5;
    border-top: 1px solid var(--line);
  }
  .hit .route, .hit .model-id { font-size: 12.5px; }
  .hit:first-child { animation: enter 0.4s ease; }
  .hit.bad { background: rgba(255, 93, 108, 0.1); }
  .route, .model-id {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .wires { display: grid; gap: 20px; margin: 0; }
  .wires div { min-width: 0; }
  dt { margin-bottom: 6px; }
  dd {
    margin: 0;
    display: flex;
    gap: 12px;
    align-items: center;
    justify-content: space-between;
    min-width: 0;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--line);
  }
  .table-wrap { overflow-x: auto; margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    text-align: left;
    padding: 12px 14px;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
    font-size: 14px;
    line-height: 1.5;
  }
  th { font-weight: 500; }
  tr.is-idle td { color: #8a95a3; }
  tr.is-idle .ok { color: #35a06f; }
  tr.is-idle .warn { color: #9a7640; }
  tr.is-active td {
    color: var(--ink);
    font-weight: 600;
    background: rgba(46, 230, 220, 0.08);
  }
  tr.is-active td:first-child {
    color: var(--accent);
    box-shadow: inset 3px 0 0 var(--accent);
  }
  tr.is-active .ok { color: var(--ok); }
  tr.is-active .warn { color: var(--warn); }
  .model-id { font-size: 13px; }
  .catalog { margin: 6px 0 0; font-size: 13px; line-height: 1.55; color: var(--muted); font-weight: 400; }
  .tag {
    display: inline-block;
    margin-right: 8px;
    font-size: 12px;
    font-weight: 500;
  }
  .tag.live { color: var(--ok); }
  .tag.catalog { color: var(--warn); }
  button.more, button.toggle {
    background: transparent;
    color: var(--accent);
    border: 0;
    padding: 0;
    margin-left: 8px;
    font: inherit;
    font-size: 13px;
    cursor: pointer;
  }
  .backend-summary {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px 20px;
    margin-bottom: 16px;
  }
  .backend-summary strong {
    font-size: 16px;
    font-weight: 650;
  }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; flex: 1 1 260px; }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 4px 12px 4px 10px;
    border: 1px solid var(--line);
    border-radius: 999px;
    font-size: 13px;
    color: var(--muted);
  }
  .chip i {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    border: 1px solid var(--muted);
  }
  .chip.ready { color: var(--ink); border-color: rgba(46, 230, 220, 0.35); }
  .chip.ready i { background: var(--accent); border-color: var(--accent); }
  .chip.active { border-color: var(--accent); }
  button.toggle {
    margin: 0;
    padding: 8px 14px;
    min-height: 0;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    font-size: 13px;
  }
  form { display: grid; gap: 20px; margin-top: 20px; }
  .form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px 24px;
  }
  .field { display: grid; gap: 8px; align-content: start; }
  .field.wide { grid-column: 1 / -1; }
  .form-grid .note { margin-top: -8px; }
  #model-count {
    color: var(--accent);
    margin-left: 6px;
    font-weight: 500;
  }
  .running {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 18px;
    padding: 14px 16px;
    border: 1px solid var(--line);
    border-left: 3px solid var(--state);
    border-radius: var(--radius-sm);
    background: var(--inset);
    font-size: 14px;
    line-height: 1.5;
    color: var(--ink);
    overflow-wrap: anywhere;
  }
  .running .orb { width: 9px; height: 9px; }
  input, select, button {
    font: inherit;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: var(--inset);
    color: var(--ink);
    padding: 11px 14px;
    min-height: 44px;
  }
  input, select { font-size: 14px; }
  select {
    appearance: none;
    background-image:
      linear-gradient(45deg, transparent 50%, var(--muted) 50%),
      linear-gradient(135deg, var(--muted) 50%, transparent 50%);
    background-position: calc(100% - 20px) 19px, calc(100% - 15px) 19px;
    background-size: 5px 5px, 5px 5px;
    background-repeat: no-repeat;
    padding-right: 36px;
  }
  button.primary {
    background: var(--accent);
    color: var(--accent-ink);
    border: 0;
    cursor: pointer;
    font-weight: 650;
    padding-inline: 28px;
  }
  button.ghost {
    background: transparent;
    color: var(--ink);
    cursor: pointer;
    flex: none;
    min-height: 0;
    padding: 7px 12px;
    font-size: 13px;
  }
  input:focus-visible, select:focus-visible, button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  button:disabled { opacity: 0.45; cursor: default; }
  .restart {
    margin-top: 28px;
    padding: 20px 0 0;
    border-top: 1px solid var(--line);
  }
  .restart .note { margin-top: 0; }
  .restart-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-top: 12px;
  }
  @media (max-width: 800px) {
    :root { --gap: 20px; --pad: 20px; }
    main { padding: 32px 16px 80px; }
    .grid, .stats, .form-grid, .well { grid-template-columns: 1fr; }
    .well-body, .scope-read { grid-column: 1; grid-row: auto; }
    .scope { margin: 8px auto 12px; }
    .scope-read { text-align: center; }
    .field.wide { grid-column: auto; }
    .stat, .stat + .stat { padding: 12px var(--pad); border-right: 0; border-bottom: 1px solid var(--line); }
    header { flex-direction: column; align-items: flex-start; margin-bottom: 28px; }
    h1 { font-size: 38px; }
    .feed-head { display: none; }
    .hit { grid-template-columns: 1fr auto; }
    .route { grid-column: 1 / -1; }
    table thead { display: none; }
    table, tbody, tr, td { display: block; width: 100%; }
    tr { padding: 8px 0 12px; }
    td { border: 0; padding: 3px 0; }
    td::before {
      content: attr(data-label);
      display: block;
      color: var(--muted);
      font-size: 12px;
    }
    .route, .model-id { white-space: normal; }
  }
</style>
</head>
<body data-level="ok">
<div class="wash" aria-hidden="true"></div>
<main>
  <header>
    <div class="brand">
      <div class="mark" aria-hidden="true"><span></span></div>
      <div>
        <p class="kicker">Local endpoint</p>
        <h1>Nex<span>us</span></h1>
        <p id="subtitle">Local gateway</p>
      </div>
    </div>
    <p class="mono bind" id="bind">—</p>
  </header>

  <div class="banner" id="banner" role="alert" hidden>
    <div class="banner-glyph" aria-hidden="true">!</div>
    <div>
      <div class="banner-title" id="banner-title"></div>
      <p class="banner-detail" id="banner-detail"></p>
      <p class="banner-extra" id="banner-extra" hidden></p>
    </div>
  </div>

  <section class="hero" aria-labelledby="status-heading">
    <div class="hero-top">
      <h2 id="status-heading">Status</h2>
    </div>
    <div class="stats" id="stats">
      <div class="stat service">
        <span>Service</span>
        <strong><i id="orb" class="orb" aria-hidden="true"></i><span id="service">—</span></strong>
        <small id="service-sub"></small>
      </div>
      <div class="stat"><span>Backend</span><strong id="backend">—</strong></div>
      <div class="stat model">
        <span>Model</span>
        <strong id="active-model" class="mono">—</strong>
        <small id="role-models" hidden></small>
      </div>
      <div class="stat"><span>Reachable from</span><strong id="bind-kind">—</strong></div>
    </div>
    <p class="note" id="status-note"></p>
  </section>

  <div class="grid">
    <section class="card" id="activity" aria-labelledby="calls-heading">
      <div class="card-head">
        <h2 id="calls-heading">Recent calls</h2>
        <span class="live" id="calls-count"></span>
      </div>
      <div class="traffic" id="traffic" aria-label="Traffic through this door">
        <p class="traffic-title">Traffic through this door</p>
        <div class="traffic-grid">
          <div><span>Calls</span><strong id="traffic-calls">—</strong></div>
          <div><span>Ok</span><strong id="traffic-ok">—</strong></div>
          <div><span>Failed</span><strong id="traffic-failed">—</strong></div>
          <div><span>Usage limit</span><strong id="traffic-limit">—</strong></div>
          <div><span>p50 / p95</span><strong id="traffic-latency">—</strong></div>
          <div><span>Tokens (when known)</span><strong id="traffic-tokens">—</strong></div>
          <div><span>Token coverage</span><strong id="traffic-token-cov">—</strong></div>
          <div><span>Since</span><strong id="traffic-since" class="mono">—</strong></div>
        </div>
        <p class="traffic-note" id="traffic-note">
          Counts for this process only. Not your Claude, ChatGPT, or Grok plan balance.
        </p>
      </div>
      <div class="well">
        <div class="scope" id="scope" role="img" aria-label="Activity. No calls yet."></div>
        <p class="scope-read" id="scope-read">No calls yet</p>
        <div class="well-body">
          <div class="quiet" id="pulse-empty">
            <p class="quiet-title">No calls yet</p>
            <p>Calls to /v1 appear here with the model, result, and time taken.</p>
          </div>
          <div id="pulse-table" hidden>
            <div class="feed-head">
              <span>Time</span><span>Route</span><span>Model</span>
              <span>Status</span><span>Took</span>
            </div>
            <ol class="feed" id="pulse"></ol>
          </div>
        </div>
      </div>
    </section>

    <section class="card" id="connection" aria-labelledby="connect-heading">
      <div class="card-head">
        <h2 id="connect-heading">Connect your app</h2>
      </div>
      <dl class="wires">
        <div>
          <dt>Base URL</dt>
          <dd>
            <code id="base-url">—</code>
            <button type="button" class="ghost" id="copy-url">Copy</button>
          </dd>
        </div>
        <div>
          <dt>Bridge key</dt>
          <dd>
            <strong id="bridge-key">—</strong>
            <button type="button" class="ghost" id="copy-key">Copy key</button>
          </dd>
        </div>
        <div>
          <dt>Active</dt>
          <dd><span id="wire-active">—</span></dd>
        </div>
      </dl>
      <p class="note" id="wire-note">Copy key reads the bridge key when you click and puts it on the
        clipboard. It is not written into the page. Provider keys are never copied from here.</p>
      <p class="note" id="wire-meta"></p>
    </section>
  </div>

  <section class="card" id="backends-card">
    <div class="card-head">
      <h2>Backends</h2>
    </div>
    <div class="backend-summary">
      <strong id="backend-summary">—</strong>
      <div class="chips" id="chips" aria-label="Backend readiness"></div>
      <button type="button" class="toggle" id="backends-toggle" aria-expanded="false"
        aria-controls="backends-table">Show all</button>
    </div>
    <div class="table-wrap" id="backends-table">
      <table>
        <thead>
          <tr><th>Backend</th><th>Ready</th><th>Models</th><th>Credential</th></tr>
        </thead>
        <tbody id="backends"></tbody>
      </table>
    </div>
    <p class="note" id="catalog-note">Catalog ids are starting guesses from Nexus, not
      entitlements. Only lists tagged live or reported by the CLI come from your account.</p>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>Configure</h2>
    </div>
    <p>Choose a backend and a model, then save.</p>
    <div class="running" id="running" data-testid="running">
      <i id="running-orb" class="orb" aria-hidden="true"></i>
      <span id="running-text">Reading status…</span>
    </div>
    <form id="config" autocomplete="off">
      <div class="form-grid">
        <div class="field">
          <label for="choice">Backend</label>
          <select id="choice" name="backend"></select>
        </div>
        <div class="field">
          <label for="model-pick">Model <span id="model-count"></span></label>
          <select id="model-pick"></select>
        </div>
        <p class="note field wide" id="choice-note"></p>
        <div class="field wide">
          <label for="model">Custom model</label>
          <input id="model" name="model" list="model-list"
            autocomplete="off" spellcheck="false">
          <datalist id="model-list"></datalist>
        </div>
        <p class="note field wide" id="model-note">Pick a known id or type one. Saved for this
          backend when a client does not name a model. A client id still wins.
          NEXUS_MODEL in the environment wins over this field.</p>
        <div class="field wide">
          <label for="key">API key</label>
          <input id="key" name="key" type="password" autocomplete="new-password"
            placeholder="Leave blank to keep the current key" value="">
        </div>
        <div class="field">
          <button class="primary" id="save" type="submit">Save</button>
        </div>
      </div>
      <p class="note" id="save-note">The key field starts empty and is cleared after save.
        A typed key is written as a private file and is not shown back.</p>
    </form>
    <div class="restart" id="restart-offer">
      <h3>Restart</h3>
      <p class="note" id="restart-note">This page cannot restart Nexus.</p>
      <div class="restart-row" id="restart-command-row" hidden>
        <code id="restart-command"></code>
        <button type="button" class="ghost" id="copy-restart">Copy command</button>
      </div>
    </div>
  </section>
</main>
<script>
const ORDER = [
  "openrouter", "openai", "anthropic", "xai", "chatgpt", "codex",
  "claude-code", "copilot", "grok-build", "antigravity", "nous"
];
const LABELS = {
  openrouter: "OpenRouter",
  openai: "OpenAI",
  anthropic: "Anthropic",
  xai: "xAI",
  nous: "Nous Portal (advanced)",
  chatgpt: "ChatGPT plan (Codex login)",
  codex: "Codex",
  "claude-code": "Claude Code",
  copilot: "GitHub Copilot",
  "grok-build": "Grok Build",
  antigravity: "Antigravity"
};
const MODELS_TTL_MS = 60000;
const MAX_BLIPS = 40;
const state = {
  health: null, healthError: false, seenAt: null,
  doctor: null, pulse: null, pulseError: false, models: {}
};
const form = { backend: "", modelDirty: false, touched: false };
const openModels = new Set();
let backendsOpen = false;
let pulseStamp = "";
let refreshing = false;
let optionStamp = "";
let modelStamp = "";

function isCli(info) {
  return !!info && (info.kind === "cli" || info.credential === "cli-login");
}

function labelOf(name) {
  return LABELS[name] || name || "—";
}

function credential(info) {
  if (!info) return "—";
  if (isCli(info)) {
    if (!info.cli_present) return "CLI not installed";
    if (info.login === "ok") return "CLI signed in";
    if (info.login === "unchecked") return "Login unchecked";
    return "Needs CLI sign-in";
  }
  if (info.key_present) {
    return info.key_source === "env" ? "Key in environment" : "Key on file";
  }
  return "Needs key";
}

function clock(ts) {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  });
}

function latency(ms) {
  if (typeof ms !== "number") return "—";
  if (ms < 1000) return ms + " ms";
  return (ms / 1000).toFixed(1) + " s";
}

function status() {
  const h = state.health;
  if (!h) {
    return {
      level: "bad", word: "Down", sub: "no answer from /health",
      title: "Service isn't responding",
      detail: "Could not load status. Nothing below is current. "
        + "Start Nexus with ./nexus start, or run ./nexus run in a terminal."
    };
  }
  const name = labelOf(h.backend);
  const model = h.model || "—";
  const info = h.backends && h.backends[h.backend];
  if (state.healthError || !h.ok) {
    return {
      level: "bad", word: "Down", sub: "no answer from /health",
      title: "Service isn't responding",
      detail: "The last good read was at " + (state.seenAt || "—") + ". Values shown are stale, "
        + "not current. Start Nexus with ./nexus start, or ./nexus run."
    };
  }
  if (h.backend_known === false) {
    return {
      level: "bad", word: "Not ready", sub: "running",
      title: "Unknown backend " + h.backend,
      detail: h.model_hint || "This backend is not supported by Nexus."
    };
  }
  if (h.ready) {
    const failure = lastChatFailure();
    if (failure) {
      return {
        level: "warn", word: "Degraded", sub: "running, but the last chat call failed",
        title: "The last chat call failed",
        detail: "POST returned " + failure.status + " at " + clock(failure.ts)
          + ". The upstream key or model may be failing even though /health says ready. "
          + "Run ./doctor.sh to check."
      };
    }
    return {
      level: "ok", word: "Ready", sub: "running",
      title: "Ready", detail: name + " · " + model
    };
  }
  if (info && !info.ready) {
    return {
      level: "bad", word: "Not ready", sub: "running, chat blocked",
      title: name + " is not ready",
      detail: h.model_hint || info.hint || "This backend cannot chat right now."
    };
  }
  const roleStatus = h.model_ready === true ? roleProblem(h) : null;
  if (roleStatus) return roleStatus;
  if (h.model_ready === null || h.model_ready === undefined) {
    const detail = isCli(info)
      ? name + " is signed in. The CLI chooses its own model, so Nexus cannot confirm "
        + model + " before a call. /health does not count that as ready."
      : (h.model_hint || "Could not confirm the model with the provider.");
    return {
      level: "warn", word: "Unconfirmed", sub: "running, model not checked",
      title: "Model not confirmed on " + name, detail: detail
    };
  }
  return {
    level: "bad", word: "Not ready", sub: "running, chat blocked",
    title: "Model not available on " + name,
    detail: h.model_hint || (model + " is not available on " + name + ".")
  };
}

const ROLE_LABELS = {
  chat: "Chat", worker: "Worker", background: "Background", vision: "Image check"
};

function configuredRoles(h) {
  const roles = h && h.roles ? h.roles : {};
  return Object.keys(ROLE_LABELS).filter((key) => roles[key] && roles[key].model);
}

function roleProblem(h) {
  const roles = h.roles || {};
  const broken = Object.keys(ROLE_LABELS).filter(
    (key) => roles[key] && "ready" in roles[key] && roles[key].ready !== true
  );
  if (!broken.length && h.roles_ready !== false) return null;
  const first = broken.length ? roles[broken[0]] : null;
  const missing = broken.some((key) => roles[key].ready === false);
  const detail = (first && first.hint) || h.roles_hint || "A per-role model is not usable.";
  if (!missing && broken.length) {
    return {
      level: "warn", word: "Unconfirmed", sub: "running, role model not checked",
      title: "A role model is not confirmed", detail: detail
    };
  }
  return {
    level: "bad", word: "Not ready", sub: "running, chat blocked",
    title: "A role model is not available", detail: detail
  };
}

function roleSummary(h) {
  if (!configuredRoles(h).length) return "";
  return Object.keys(ROLE_LABELS)
    .map((key) => ROLE_LABELS[key] + " " + ((h.roles[key] && h.roles[key].model) || "from OS3"))
    .join(" · ");
}

function lastChatFailure() {
  const entries = state.pulse && Array.isArray(state.pulse.entries) ? state.pulse.entries : [];
  for (let i = 0; i < entries.length; i += 1) {
    const entry = entries[i];
    const route = String(entry.route || "");
    if (route.indexOf("POST /v1/chat/completions") !== 0 && route.indexOf("POST /v1/messages") !== 0) {
      continue;
    }
    if (typeof entry.status === "number" && entry.status >= 500) return entry;
    return null;
  }
  return null;
}

function renderBanner(v) {
  const banner = document.getElementById("banner");
  const extra = document.getElementById("banner-extra");
  if (v.level === "ok") {
    banner.hidden = true;
    return;
  }
  const failure = lastChatFailure();
  const note = failure && v.word !== "Degraded"
    ? "Last chat call: " + failure.status + " at " + clock(failure.ts) + "."
    : "";
  banner.hidden = false;
  banner.className = v.level === "warn" ? "banner warn" : "banner";
  document.getElementById("banner-title").textContent = v.title;
  document.getElementById("banner-detail").textContent = v.detail;
  extra.textContent = note;
  extra.hidden = !note;
}

function renderStatus(v) {
  const h = state.health;
  document.body.dataset.level = v.level;
  document.title = "Nexus · " + v.word;
  const stats = document.getElementById("stats");
  stats.className = state.healthError ? "stats stale" : "stats";
  document.getElementById("service").textContent = v.word;
  document.getElementById("service").className = v.level;
  document.getElementById("service-sub").textContent = v.sub;
  document.getElementById("orb").className = "orb " + v.level;
  document.getElementById("scope").className = v.word === "Down" ? "scope down" : "scope";
  if (!h) return;
  document.getElementById("subtitle").textContent =
    "Local gateway" + (h.version ? " · v" + h.version : "");
  document.getElementById("bind").textContent = h.bind || "";
  document.getElementById("backend").textContent = labelOf(h.backend);
  document.getElementById("active-model").textContent = h.model || "—";
  const roleLine = document.getElementById("role-models");
  roleLine.textContent = roleSummary(h);
  roleLine.hidden = !roleLine.textContent;
  document.getElementById("bind-kind").textContent = h.loopback_enforced ? "This computer only" : "Not limited to this computer";
  document.getElementById("status-note").textContent =
    v.level === "ok" ? "" : "See the message above for why.";
  document.getElementById("base-url").textContent = baseUrl(h);
  document.getElementById("wire-active").textContent = labelOf(h.backend) + " · " + (h.model || "—");
  showRestart(h.restart_note, h.restart_command);
}

function renderRunning(v) {
  const h = state.health;
  const orb = document.getElementById("running-orb");
  const text = document.getElementById("running-text");
  orb.className = "orb " + v.level;
  if (!h) {
    text.textContent = "Status: unknown. " + v.word + ".";
    return;
  }
  const running = labelOf(h.backend) + " · " + (h.model || "—") + " · " + v.word;
  const chosen = document.getElementById("choice").value;
  if (chosen && chosen !== h.backend) {
    text.textContent = "Running: " + running + ". Draft: " + labelOf(chosen)
      + " is only a draft until you save it.";
  } else {
    text.textContent = "Running: " + running + ". Same as Status.";
  }
}

function cell(row, text, label) {
  const td = document.createElement("td");
  td.dataset.label = label;
  td.textContent = text;
  row.appendChild(td);
  return td;
}

function sourceTag(info) {
  const tag = document.createElement("span");
  const fromCli = info.models_source === "cli";
  tag.className = "tag " + (fromCli ? "live" : "catalog");
  tag.textContent = fromCli ? "reported by CLI" : "Suggested";
  return tag;
}

function renderModels(cellEl, name, info) {
  cellEl.replaceChildren();
  cellEl.dataset.label = "Models";
  const models = info && Array.isArray(info.models) ? info.models.slice() : [];
  const fallback = info && info.default_model ? info.default_model : "";
  const cli = info && info.cli_model ? info.cli_model : "";
  const full = models.join(", ");
  cellEl.title = [full, info && info.models_hint].filter(Boolean).join(" — ");
  const primary = document.createElement("div");
  primary.className = "model-id";
  primary.textContent = fallback || cli || (models[0] || "—");
  cellEl.appendChild(primary);
  if (isCli(info)) {
    const resolved = document.createElement("p");
    resolved.className = "catalog";
    var cliLine = "CLI not installed";
    if (cli && cli !== fallback) cliLine = "CLI resolved " + cli;
    else if (cli) cliLine = "From CLI";
    else if (info.cli_present) cliLine = "CLI model not reported";
    resolved.textContent = cliLine;
    cellEl.appendChild(resolved);
  }
  if (!models.length) return;
  const catalog = document.createElement("p");
  catalog.className = "catalog";
  catalog.appendChild(sourceTag(info));
  const limit = 3;
  const open = openModels.has(name);
  if (models.length <= limit || open) {
    catalog.appendChild(document.createTextNode(models.join(", ")));
    if (models.length > limit) {
      const less = document.createElement("button");
      less.type = "button";
      less.className = "more";
      less.textContent = "show less";
      less.addEventListener("click", function () {
        openModels.delete(name);
        render();
      });
      catalog.appendChild(less);
    }
  } else {
    const extra = models.length - limit;
    catalog.appendChild(document.createTextNode(models.slice(0, limit).join(", ")));
    const more = document.createElement("button");
    more.type = "button";
    more.className = "more";
    more.textContent = limit + " + " + extra + " more";
    more.title = full;
    more.setAttribute("aria-expanded", "false");
    more.addEventListener("click", function () {
      openModels.add(name);
      render();
    });
    catalog.appendChild(more);
  }
  cellEl.appendChild(catalog);
}

function renderBackends(names, v) {
  const h = state.health;
  const ready = function (name) {
    const info = h.backends[name];
    return name === h.backend ? !!info.ready && v.level === "ok" : !!info.ready;
  };
  const body = document.getElementById("backends");
  const chips = document.getElementById("chips");
  body.replaceChildren();
  chips.replaceChildren();
  const readyCount = names.filter(ready).length;
  document.getElementById("backend-summary").textContent =
    labelOf(h.backend) + " active · " + readyCount + " of " + names.length + " ready";
  names.forEach(function (name) {
    const info = h.backends[name];
    const chip = document.createElement("span");
    chip.className = "chip" + (ready(name) ? " ready" : "") + (name === h.backend ? " active" : "");
    const dot = document.createElement("i");
    chip.append(dot, document.createTextNode(labelOf(name)));
    chip.title = ready(name) ? "Ready" : "Not ready";
    chips.appendChild(chip);
  });
  const toggle = document.getElementById("backends-toggle");
  const hidden = names.length - 1;
  toggle.textContent = backendsOpen ? "Collapse" : "Show all " + names.length;
  toggle.setAttribute("aria-expanded", backendsOpen ? "true" : "false");
  toggle.hidden = hidden < 1;
  names.forEach(function (name) {
    if (!backendsOpen && name !== h.backend) return;
    const info = h.backends[name];
    const row = document.createElement("tr");
    row.className = name === h.backend ? "is-active" : "is-idle";
    if (ready(name)) row.className += " is-ready";
    cell(row, labelOf(name), "Backend");
    const word = name === h.backend && !ready(name) && info.ready ? v.word : "Not ready";
    const statusCell = cell(row, ready(name) ? "Ready" : word, "Ready");
    statusCell.className = ready(name) ? "ok" : "warn";
    const models = document.createElement("td");
    row.appendChild(models);
    renderModels(models, name, info);
    cell(row, credential(info), "Credential");
    body.appendChild(row);
  });
}

function hash(text) {
  let value = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    value ^= text.charCodeAt(i);
    value = Math.imul(value, 16777619);
  }
  return value >>> 0;
}

function renderScope(entries) {
  const scope = document.getElementById("scope");
  scope.querySelectorAll(".blip").forEach(function (node) { node.remove(); });
  const shown = entries.slice(0, MAX_BLIPS);
  let bad = 0;
  shown.forEach(function (entry, index) {
    const failed = typeof entry.status === "number" && entry.status >= 400;
    if (failed) bad += 1;
    const angle = (hash(String(entry.ts) + String(entry.route)) % 360) * Math.PI / 180;
    const radius = 14 + (index / MAX_BLIPS) * 32;
    const blip = document.createElement("span");
    blip.className = "blip" + (failed ? " bad" : "") + (index === 0 ? " fresh" : "");
    blip.style.left = (50 + radius * Math.cos(angle)) + "%";
    blip.style.top = (50 + radius * Math.sin(angle)) + "%";
    blip.style.opacity = String(Math.max(0.25, 1 - index / (MAX_BLIPS * 1.2)));
    blip.style.color = failed ? "#ff5d6c" : "#2ee6dc";
    scope.appendChild(blip);
  });
  const read = shown.length
    ? "Activity · " + shown.length + " calls · " + bad + " failed"
    : "No calls yet";
  document.getElementById("scope-read").textContent = read;
  scope.setAttribute("aria-label", read.replace("Activity · ", "Activity scope. "));
}

function renderTraffic(traffic) {
  const calls = document.getElementById("traffic-calls");
  const ok = document.getElementById("traffic-ok");
  const failed = document.getElementById("traffic-failed");
  const limit = document.getElementById("traffic-limit");
  const latency = document.getElementById("traffic-latency");
  const tokens = document.getElementById("traffic-tokens");
  const cov = document.getElementById("traffic-token-cov");
  const since = document.getElementById("traffic-since");
  const note = document.getElementById("traffic-note");
  if (!traffic || typeof traffic !== "object") {
    calls.textContent = "—";
    ok.textContent = "—";
    failed.textContent = "—";
    limit.textContent = "—";
    limit.className = "";
    latency.textContent = "—";
    tokens.textContent = "—";
    cov.textContent = "—";
    since.textContent = "—";
    note.textContent =
      "Counts for this process only. Not your Claude, ChatGPT, or Grok plan balance.";
    return;
  }
  const failCount = (traffic.client_error || 0) + (traffic.server_error || 0);
  calls.textContent = String(traffic.calls == null ? "—" : traffic.calls);
  ok.textContent = String(traffic.ok == null ? "—" : traffic.ok);
  failed.textContent = String(failCount);
  failed.className = failCount ? "bad" : "";
  const limits = traffic.usage_limit || 0;
  limit.textContent = String(limits);
  limit.className = limits ? "bad" : "";
  const lat = traffic.latency_ms || {};
  if (lat.p50 == null && lat.p95 == null) {
    latency.textContent = "—";
  } else {
    latency.textContent =
      (lat.p50 == null ? "—" : lat.p50 + "ms") +
      " / " +
      (lat.p95 == null ? "—" : lat.p95 + "ms");
  }
  const tok = traffic.tokens || {};
  if (tok.total == null) {
    tokens.textContent = "—";
  } else {
    tokens.textContent = String(tok.total);
  }
  if (tok.known_calls == null) {
    cov.textContent = "—";
  } else {
    cov.textContent = tok.known_calls + " of " + (traffic.calls || 0) + " calls";
  }
  since.textContent = traffic.since ? clock(traffic.since) : "—";
  note.textContent = traffic.note
    || "Counts for this process only. Not your Claude, ChatGPT, or Grok plan balance.";
}

function renderPulse(payload) {
  const empty = document.getElementById("pulse-empty");
  const table = document.getElementById("pulse-table");
  const list = document.getElementById("pulse");
  const entries = payload && Array.isArray(payload.entries) ? payload.entries : [];
  renderTraffic(payload && payload.traffic);
  const count = document.getElementById("calls-count");
  count.textContent = state.pulseError || !entries.length
    ? ""
    : entries.length + (entries.length === 1 ? " call" : " calls");
  if (state.pulseError) {
    empty.hidden = false;
    table.hidden = true;
    empty.querySelector(".quiet-title").textContent = "Recent calls could not be loaded";
    empty.querySelectorAll("p")[1].textContent =
      "The call list could not be loaded. Status above is still live.";
    return;
  }
  empty.querySelector(".quiet-title").textContent = "No calls yet";
  empty.querySelectorAll("p")[1].textContent =
    "Calls to /v1 appear here with the model, result, and time taken.";
  const stamp = JSON.stringify({ entries: entries, traffic: payload && payload.traffic });
  if (!entries.length) {
    pulseStamp = stamp;
    empty.hidden = false;
    table.hidden = true;
    list.replaceChildren();
    renderScope([]);
    return;
  }
  empty.hidden = true;
  table.hidden = false;
  if (stamp === pulseStamp) return;
  pulseStamp = stamp;
  renderScope(entries);
  list.replaceChildren();
  entries.forEach(function (entry) {
    const row = document.createElement("li");
    const bad = typeof entry.status === "number" && entry.status >= 400;
    row.className = bad ? "hit bad" : "hit";
    const time = document.createElement("time");
    time.textContent = clock(entry.ts);
    const route = document.createElement("span");
    route.className = "route";
    route.textContent = entry.route || "—";
    const model = document.createElement("span");
    model.className = "model-id";
    model.textContent = entry.model || "—";
    const status = document.createElement("span");
    status.className = bad ? "bad" : "ok";
    if (entry.usage_limit) {
      status.textContent = (entry.status == null ? "—" : String(entry.status)) + " limit";
    } else {
      status.textContent = entry.status == null ? "—" : String(entry.status);
    }
    const took = document.createElement("span");
    took.textContent = latency(entry.latency_ms);
    row.append(time, route, model, status, took);
    list.appendChild(row);
  });
}

function showRestart(note, command) {
  document.getElementById("restart-note").textContent = note
    || "This page cannot restart Nexus.";
  const row = document.getElementById("restart-command-row");
  const code = document.getElementById("restart-command");
  if (command) {
    code.textContent = command;
    row.hidden = false;
  } else {
    code.textContent = "";
    row.hidden = true;
  }
}

function baseUrl(health) {
  const bind = health && health.bind ? health.bind : window.location.host;
  return "http://" + bind + "/v1";
}

function render() {
  const health = state.health;
  const v = status();
  renderBanner(v);
  renderStatus(v);
  if (!health) {
    renderRunning(v);
    return;
  }
  const select = document.getElementById("choice");
  const names = ORDER.filter(function (name) { return health.backends && health.backends[name]; });
  const signature = names.join(",");
  if (signature !== optionStamp) {
    optionStamp = signature;
    select.replaceChildren();
    names.forEach(function (name) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = labelOf(name);
      select.appendChild(option);
    });
  }
  const draftPicked = form.touched && names.indexOf(select.value) >= 0;
  if (!draftPicked && names.indexOf(health.backend) >= 0) {
    select.value = health.backend;
  }
  renderBackends(names, v);
  fillChoice();

  if (state.doctor) {
    const present = state.doctor.bridge_key && state.doctor.bridge_key.present;
    const mode = state.doctor.bridge_key && state.doctor.bridge_key.mode;
    document.getElementById("bridge-key").textContent = present
      ? ("Present" + (mode ? " · mode " + mode : ""))
      : "Missing";
    document.getElementById("copy-key").disabled = !present;
    const route = health.routes && health.routes["/v1/messages"];
    const auth = state.doctor.auth || "bearer";
    document.getElementById("wire-meta").textContent = route
      ? ("Auth " + auth + " · Messages " + route.mode)
      : ("Auth " + auth);
  }
  renderPulse(state.pulse);
  renderRunning(v);
}

function pickEntries(name, info) {
  const cached = state.models[name];
  if (cached && Date.now() - cached.at < MODELS_TTL_MS && cached.payload
      && cached.payload.backend === name) {
    return cached.payload;
  }
  const status = info && info.models_source === "cli" ? "cli" : "catalog";
  const seen = {};
  const models = [];
  const raw = info && Array.isArray(info.models) ? info.models : [];
  raw.forEach(function (id) {
    if (typeof id !== "string" || !id || seen[id]) return;
    seen[id] = true;
    models.push({ id: id, status: status });
  });
  const fallback = info && info.default_model;
  if (typeof fallback === "string" && fallback && !seen[fallback]) {
    models.unshift({ id: fallback, status: "catalog" });
  }
  return {
    backend: name,
    source: status,
    hint: info && info.models_hint ? info.models_hint : "",
    models: models
  };
}

function optionLabel(entry) {
  if (entry.status === "live" || entry.status === "cli") return entry.id;
  if (entry.status === "unverified") return entry.id + "  ·  unverified";
  return entry.id + "  ·  catalog";
}

function sourceWord(payload) {
  const n = payload.models.length;
  const noun = n === 1 ? " id" : " ids";
  if (payload.source === "live") return "From your account · " + n + noun;
  if (payload.source === "cli") return "From CLI · " + n + noun;
  return "Catalog · " + n + (n === 1 ? " starting guess" : " starting guesses");
}

function syncPick() {
  const pick = document.getElementById("model-pick");
  const value = document.getElementById("model").value;
  var matched = false;
  Array.prototype.forEach.call(pick.options, function (opt) {
    if (opt.value && opt.value === value) matched = true;
  });
  pick.value = matched ? value : "";
}

function fillModels(name, info) {
  const list = document.getElementById("model-list");
  const pick = document.getElementById("model-pick");
  const payload = pickEntries(name, info);
  const stamp = name + "\n" + payload.source + "\n"
    + payload.models.map(function (m) { return m.id + ":" + m.status; }).join("\n");
  if (stamp !== modelStamp) {
    modelStamp = stamp;
    list.replaceChildren();
    pick.replaceChildren();
    payload.models.forEach(function (entry) {
      const option = document.createElement("option");
      option.value = entry.id;
      list.appendChild(option);
      const choice = document.createElement("option");
      choice.value = entry.id;
      choice.textContent = optionLabel(entry);
      pick.appendChild(choice);
    });
    const custom = document.createElement("option");
    custom.value = "";
    custom.textContent = payload.models.length ? "Custom model…" : "No models listed";
    pick.appendChild(custom);
  }
  document.getElementById("model-count").textContent = sourceWord(payload);
  const note = payload.source === "live" || payload.source === "cli"
    ? payload.hint
    : "Suggested models — they may not be on your plan. "
      + (payload.hint || "");
  document.getElementById("model-note").textContent =
    (note ? note.trim() + " " : "")
    + "Saved for this backend when a client does not name a model. A client id still wins. "
    + "NEXUS_MODEL in the environment wins over this field.";
  syncPick();
}

function loadModels(name) {
  const cached = state.models[name];
  if (cached && (cached.pending || Date.now() - cached.at < MODELS_TTL_MS)) return;
  state.models[name] = { at: Date.now(), pending: true, payload: null };
  readJson("/dashboard/models?backend=" + encodeURIComponent(name)).then(function (payload) {
    state.models[name] = { at: Date.now(), pending: false, payload: payload };
  }).catch(function () {
    state.models[name] = { at: Date.now(), pending: false, payload: null };
  }).then(function () {
    if (document.getElementById("choice").value === name) fillChoice();
  });
}

function fillChoice() {
  const health = state.health;
  const name = document.getElementById("choice").value;
  const info = health && health.backends && health.backends[name];
  const model = document.getElementById("model");
  const key = document.getElementById("key");
  if (form.backend !== name) {
    var keepTyped = form.modelDirty && form.backend !== "";
    form.backend = name;
    form.modelDirty = false;
    if (!keepTyped) {
      const running = health && name === health.backend && health.model;
      model.value = running || (info && info.default_model ? info.default_model : "");
    }
    key.value = "";
  }
  if (name) loadModels(name);
  fillModels(name, info);
  key.disabled = isCli(info);
  document.getElementById("choice-note").textContent = info && info.hint ? info.hint : "";
  if (health) renderRunning(status());
}

async function readJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(url);
  return response.json();
}

function stamp() {
  return new Date().toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  });
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state.health = await readJson("/health");
    state.healthError = false;
    state.seenAt = stamp();
    try { state.doctor = await readJson("/doctor"); } catch (err) { /* keep the last doctor */ }
    try {
      state.pulse = await readJson("/dashboard/pulse");
      state.pulseError = false;
    } catch (err) {
      state.pulseError = true;
    }
  } catch (err) {
    state.healthError = true;
  } finally {
    refreshing = false;
  }
  render();
}

async function copyText(text) {
  if (!navigator.clipboard || !navigator.clipboard.writeText) return false;
  await navigator.clipboard.writeText(text);
  return true;
}

document.getElementById("model").addEventListener("input", function () {
  form.modelDirty = true;
  syncPick();
});
document.getElementById("model-pick").addEventListener("change", function () {
  const pick = document.getElementById("model-pick");
  const model = document.getElementById("model");
  if (!pick.value) {
    model.focus();
    return;
  }
  model.value = pick.value;
  form.modelDirty = true;
});
document.getElementById("choice").addEventListener("change", function () {
  form.touched = true;
  form.backend = "";
  form.modelDirty = false;
  fillChoice();
});
document.getElementById("backends-toggle").addEventListener("click", function () {
  backendsOpen = !backendsOpen;
  render();
});
document.getElementById("copy-url").addEventListener("click", function () {
  const note = document.getElementById("wire-note");
  const url = document.getElementById("base-url").textContent;
  copyText(url).then(function (ok) {
    note.textContent = ok
      ? "Base URL copied."
      : "Select the base URL and copy it. Clipboard is unavailable.";
  }).catch(function () {
    note.textContent = "Select the base URL and copy it. Clipboard is unavailable.";
  });
});
document.getElementById("copy-key").addEventListener("click", function () {
  const note = document.getElementById("wire-note");
  note.textContent = "Reading the bridge key…";
  fetch("/dashboard/bridge-key", { cache: "no-store" }).then(function (response) {
    return response.json().then(function (payload) {
      return { ok: response.ok, payload: payload };
    });
  }).then(function (result) {
    const key = result.payload && result.payload.key;
    if (!result.ok || !result.payload.present || typeof key !== "string" || !key) {
      note.textContent = "No bridge key file yet.";
      return;
    }
    return copyText(key).then(function (ok) {
      note.textContent = ok
        ? "Bridge key copied. It was not shown on this page."
        : "Clipboard unavailable. Read the key file on this computer.";
    });
  }).catch(function () {
    note.textContent = "Could not copy the bridge key.";
  });
});
document.getElementById("copy-restart").addEventListener("click", function () {
  const command = document.getElementById("restart-command").textContent;
  const note = document.getElementById("restart-note");
  copyText(command).then(function (ok) {
    if (!ok) return;
    note.textContent =
      "Restart command copied. Run it in another terminal. This page will not run it.";
  });
});
document.getElementById("config").addEventListener("submit", function (event) {
  event.preventDefault();
  const note = document.getElementById("save-note");
  const key = document.getElementById("key");
  const button = document.getElementById("save");
  const body = {
    backend: document.getElementById("choice").value,
    model: document.getElementById("model").value
  };
  if (key.value) body.api_key = key.value;
  key.value = "";
  button.disabled = true;
  note.textContent = "Saving…";
  fetch("/dashboard/config", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  }).then(function (response) {
    return response.json().then(function (payload) {
      return { ok: response.ok, payload: payload };
    });
  }).then(function (result) {
    if (!result.ok || !result.payload.ok) {
      note.textContent = (result.payload && result.payload.error) || "Could not save.";
      return;
    }
    const extra = result.payload.model_note ? " " + result.payload.model_note : "";
    note.textContent = "Saved " + result.payload.backend + "." + extra;
    showRestart(result.payload.restart, result.payload.restart_command);
    form.backend = "";
    form.modelDirty = false;
    form.touched = false;
    state.models = {};
    return refresh();
  }).catch(function () {
    note.textContent = "Could not save.";
  }).then(function () {
    button.disabled = false;
  });
});

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
