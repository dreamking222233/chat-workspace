/**
 * Keep browser requests same-origin in production so the web server can proxy
 * them to the API. Vite development still uses the standalone local backend.
 */
const configuredBase = import.meta.env.VITE_API_BASE_URL?.trim()

export const API_BASE = configuredBase || (import.meta.env.DEV ? 'http://localhost:8000/api/v1' : '/api/v1')
