import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { readFileSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))

function getBackendPort() {
  try {
    const portFile = resolve(__dirname, '../.flask_port')
    return parseInt(readFileSync(portFile, 'utf-8').trim(), 10)
  } catch {
    return 5050
  }
}

const backendUrl = `http://127.0.0.1:${getBackendPort()}`

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/status': backendUrl,
      '/run': backendUrl,
      '/stop': backendUrl,
      '/job': backendUrl,
      '/connect': backendUrl,
      '/health': backendUrl,
      '/sources': backendUrl,
      '/outreach': backendUrl,
      '/webhook': backendUrl,
    },
  },
})
