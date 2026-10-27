import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server on 5174 so it can run beside the dashboard's 5173, proxying to
// the station backend on 8100 rather than the dashboard's 8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8100', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8100', ws: true },
    },
  },
  css: { preprocessorOptions: { scss: { loadPaths: ['node_modules'], quietDeps: true } } },
  build: { outDir: 'dist' },
})
