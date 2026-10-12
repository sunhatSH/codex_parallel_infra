import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev proxy: frontend on :5173, hub API on :8787 — no CORS pain in dev.
// In prod, `npm run build` emits static files served by any static host;
// point VITE_API_BASE at the hub URL (or leave empty for same-origin).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
    },
  },
})
