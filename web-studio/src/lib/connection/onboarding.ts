import { resolveConnectionDataRoot } from './probe'
import type { ConnectionDraft } from '#/lib/connection/types'
import { createConnectionHealthHeaders } from './auth'
import { createClient } from '#/gen/ov-client/client'
import { getOvResult } from '#/lib/ov-client'
import {
  resolveServerAuthMode,
  fetchConnectionHealth,
  isUsableServerHealth,
  readServerIdentity,
} from './health'
import {
  isValidConnectionUrl,
  normalizeProfileConnection,
  PROFILES_STORAGE_KEY,
} from '#/lib/connection/profiles'

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
  const health = await fetchConnectionHealth(connection, signal)
  if (!isUsableServerHealth(health))
    throw new StudioSetupError('invalidService')
  const mode = resolveServerAuthMode(health)
  if (
    mode !== 'dev' &&
    !connection.apiKey &&
    !(connection.serviceSelection === 'opensource' && connection.adminApiKey)
  )
    throw new StudioSetupError('keyRequired')
  if (connection.serviceSelection === 'volcengine') {
    connection.accountId = ''
    connection.userId = ''
  }
  const identity = readServerIdentity(health)
  if (identity.accountId) connection.accountId = identity.accountId
  if (identity.userId) connection.userId = identity.userId
  const dataClient = createClient({
    baseURL: connection.baseUrl,
    headers: createConnectionHealthHeaders(connection, 'data'),
    timeout: 10000,
    throwOnError: true,
  })
  const managementOnly =
    mode === 'api_key' &&
    connection.serviceSelection === 'opensource' &&
    !connection.apiKey &&
    Boolean(connection.adminApiKey)
  if (managementOnly) {
    // ROOT cannot call tenant data APIs. Verify control access before letting
    // a new administrator open Users and mint a separate user credential.
    if (health.role !== 'root' && health.role !== 'admin')
      throw new StudioSetupError('keyRequired')
    await getOvResult(
      dataClient.get({
        url:
          health.role === 'root'
            ? '/api/v1/admin/accounts'
            : '/api/v1/admin/accounts/{account_id}/users',
        path: { account_id: connection.accountId },
        signal,
      }),
    )
  } else
    await getOvResult(
      dataClient.get({
        url: '/api/v1/fs/ls',
        query: {
          uri: resolveConnectionDataRoot(connection, 'root'),
          node_limit: 1,
          output: 'agent',
        },
        signal,
      }),
    )
  signal.throwIfAborted()
  return connection
}

export function studioSetupDestination(
  connection: ConnectionDraft,
): '/filesystem' | '/users' | '/home' {
  if (connection.serviceSelection === 'volcengine') return '/filesystem'
  return !connection.apiKey && connection.adminApiKey ? '/users' : '/home'
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
