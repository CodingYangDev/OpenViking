import type { ConnectionDraft } from '#/hooks/use-app-connection'
import { createConnectionHealthHeaders } from '#/hooks/use-app-connection'
import { createClient } from '#/gen/ov-client/client'
import { getOvResult } from '#/lib/ov-client'
import { resolveServerAuthMode } from '#/hooks/use-server-mode'
import {
  isValidConnectionUrl,
  normalizeProfileConnection,
  PROFILES_STORAGE_KEY,
} from './connection-profiles'

export const SETUP_STORAGE_KEY = 'ov_studio_setup'
export class StudioSetupError extends Error {
  constructor(public readonly code: 'invalidService' | 'keyRequired') {
    super(code)
  }
}

export const CLOUD_SETUP_URL =
  'https://api.vikingdb.cn-beijing.volces.com/openviking'

function hasConnection(value: unknown, completed = false): boolean {
  if (!value || typeof value !== 'object') return false
  const c = value as Partial<ConnectionDraft>
  return (
    typeof c.baseUrl === 'string' &&
    isValidConnectionUrl(c.baseUrl) &&
    (completed ||
      Boolean(
        (typeof c.apiKey === 'string' && c.apiKey.trim()) ||
        (typeof c.adminApiKey === 'string' && c.adminApiKey.trim()),
      ) ||
      c.baseUrl.replace(/\/+$/, '') !== window.location.origin)
  )
}

export function needsStudioSetup(): boolean {
  try {
    const state = localStorage.getItem(SETUP_STORAGE_KEY)
    if (state === 'pending') return true
    const profiles: unknown = JSON.parse(
      localStorage.getItem(PROFILES_STORAGE_KEY) || 'null',
    )
    if (
      profiles &&
      typeof profiles === 'object' &&
      'profiles' in profiles &&
      Array.isArray(profiles.profiles) &&
      profiles.profiles.some(
        (p) =>
          p &&
          typeof p === 'object' &&
          hasConnection(p.connection, state === 'complete'),
      )
    )
      return false
    if (
      hasConnection(
        JSON.parse(localStorage.getItem('ov_console_connection') || 'null'),
        state === 'complete',
      )
    )
      return false
    if (sessionStorage.getItem('ov_console_api_key')?.trim()) return false
  } catch {
    /* Malformed or unavailable storage is handled by the setup screen. */
  }
  return !(
    import.meta.env.VITE_OV_BASE_URL ||
    import.meta.env.VITE_OV_API_KEY ||
    import.meta.env.VITE_OV_ADMIN_API_KEY
  )
}

export function markStudioSetupPending() {
  try {
    localStorage.setItem(SETUP_STORAGE_KEY, 'pending')
  } catch {
    /* Show the setup even without storage. */
  }
}

export async function verifyStudioSetup(
  draft: ConnectionDraft,
  signal: AbortSignal,
): Promise<ConnectionDraft> {
  const connection = normalizeProfileConnection({
    ...draft,
    baseUrl: draft.baseUrl.trim().replace(/\/+$/, ''),
    apiKey: draft.apiKey.trim(),
    adminApiKey: draft.adminApiKey.trim(),
  })
  if (!isValidConnectionUrl(connection.baseUrl))
    throw new Error('Invalid server URL')
  if (connection.serviceSelection === 'volcengine') connection.adminApiKey = ''
  const client = createClient({
    baseURL: connection.baseUrl,
    headers: createConnectionHealthHeaders(connection, 'data'),
    timeout: 10000,
    throwOnError: true,
  })
  const response = await client.get({ url: '/health', signal })
  const health = response.data as Record<string, unknown> | null
  if (
    !health ||
    typeof health !== 'object' ||
    health.status === 'error' ||
    health.error ||
    (typeof health.version !== 'string' && typeof health.auth_mode !== 'string')
  )
    throw new StudioSetupError('invalidService')
  const mode = resolveServerAuthMode(health)
  if (
    mode !== 'dev' &&
    !connection.apiKey &&
    !(mode === 'trusted' && connection.adminApiKey)
  )
    throw new StudioSetupError('keyRequired')
  if (connection.serviceSelection === 'volcengine') {
    connection.accountId = ''
    connection.userId = ''
  }
  if (typeof health.account_id === 'string' && health.account_id)
    connection.accountId = health.account_id
  if (typeof health.user_id === 'string' && health.user_id)
    connection.userId = health.user_id
  const dataClient = createClient({
    baseURL: connection.baseUrl,
    headers: createConnectionHealthHeaders(connection, 'data'),
    timeout: 10000,
    throwOnError: true,
  })
  await getOvResult(
    dataClient.get({
      url: '/api/v1/fs/ls',
      query: {
        uri:
          connection.serviceSelection === 'volcengine'
            ? 'viking://~/'
            : 'viking://',
        node_limit: 1,
        output: 'agent',
      },
      signal,
    }),
  )
  signal.throwIfAborted()
  return connection
}

export function completeStudioSetup(connection: ConnectionDraft, name: string) {
  // Do not enter the workspace if the browser cannot retain this connection.
  localStorage.setItem(
    PROFILES_STORAGE_KEY,
    JSON.stringify({
      activeId: 'default',
      profiles: [{ id: 'default', name, connection }],
    }),
  )
  localStorage.setItem('ov_console_connection', JSON.stringify(connection))
  localStorage.setItem(SETUP_STORAGE_KEY, 'complete')
}
