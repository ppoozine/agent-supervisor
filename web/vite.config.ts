import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

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
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
  test: {
    environment: 'jsdom',
    restoreMocks: true,
  },
})
