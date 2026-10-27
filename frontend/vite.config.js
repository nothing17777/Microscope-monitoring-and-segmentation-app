import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev the Vite server proxies the API and the WebSocket to the backend,
// so the dashboard works at :5173 with no CORS or URL juggling.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
  build: { outDir: 'dist' },
})
