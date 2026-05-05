import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/status': 'http://127.0.0.1:5050',
      '/run': 'http://127.0.0.1:5050',
      '/stop': 'http://127.0.0.1:5050',
      '/job': 'http://127.0.0.1:5050',
      '/connect': 'http://127.0.0.1:5050',
      '/health': 'http://127.0.0.1:5050',
    },
  },
})
