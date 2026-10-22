/// <reference types="vitest" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The UI is served by the door at /, so every request is relative and
// same-origin. In dev, Vite forwards those paths to a door you started for
// development, e.g. NEXUS_DOOR=http://127.0.0.1:11436 npm run dev
// (NEXUS_PORT=11436 python server.py; OS3_BRIDGE_PORT still works). Default: the standard door.
const DOOR = process.env.NEXUS_DOOR ?? 'http://127.0.0.1:11435';
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(DOOR)) {
  throw new Error(`NEXUS_DOOR must be http://127.0.0.1:<port>, got ${DOOR}`);
}
const proxied = ['/health', '/doctor', '/dashboard', '/v1'];

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    host: '127.0.0.1',
    port: 5173,
    // Same-origin only: no CORS headers, even in dev.
    cors: false,
    proxy: Object.fromEntries(
      proxied.map((path) => [
        path,
        {
          target: DOOR,
          changeOrigin: true,
          // The door's guard compares Origin with Host; present the door's own origin.
          headers: { origin: DOOR },
        },
      ]),
    ),
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
