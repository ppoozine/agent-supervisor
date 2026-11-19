import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// 開發時前端在 5173，把 /api 轉給本機的 daemon；正式使用時由 daemon 直接提供 dist。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
  test: {
    environment: 'jsdom',
    restoreMocks: true,
  },
})
