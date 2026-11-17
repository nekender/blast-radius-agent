import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Dev: `npm run dev` proxies the API to the Python server (python -m blast_radius ui).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': 'http://localhost:8700',
      '/artifacts': 'http://localhost:8700',
    },
  },
})
