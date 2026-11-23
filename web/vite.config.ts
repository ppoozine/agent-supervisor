import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import type { ProxyOptions } from 'vite'
import { defineConfig } from 'vitest/config'

const DAEMON = 'http://127.0.0.1:8765'

// Just what the proxy hook below touches; the full types would need @types/node.
interface ProxyEvents {
  on(
    event: 'proxyReqWs',
    listener: (
      outgoing: { setHeader(name: string, value: string): void },
      incoming: { headers: Record<string, string | string[] | undefined> },
    ) => void,
  ): void
}

/**
 * Forward /api, including the task shell's WebSocket, to the daemon at `target`.
 *
 * The daemon only accepts a shell connection whose Origin is its own address and matches Host. So
 * the proxy rewrites Host, and rewrites Origin only for the dev server's own page; any other page
 * keeps its Origin and the daemon refuses it, so the dev server cannot hand out shells to other
 * local sites.
 */
export function daemonProxy(target: string): ProxyOptions {
  return {
    target,
    ws: true,
    changeOrigin: true,
    configure: (proxy) => {
      ;(proxy as unknown as ProxyEvents).on('proxyReqWs', (outgoing, incoming) => {
        const { origin, host } = incoming.headers
        if (typeof origin !== 'string' || typeof host !== 'string') return
        if (URL.canParse(origin) && new URL(origin).host === host) outgoing.setHeader('origin', target)
      })
    },
  }
}

// During development the frontend runs on 5173 and forwards /api to the local daemon. The build
// goes into the Python package, where the daemon serves it from, and release packages carry it.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../src/agent_supervisor/web_dist',
    // The directory is outside web/, so Vite only empties it when told to.
    emptyOutDir: true,
  },
  server: {
    proxy: { '/api': daemonProxy(DAEMON) },
  },
  test: {
    environment: 'jsdom',
    restoreMocks: true,
  },
})
