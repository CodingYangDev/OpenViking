// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  completeStudioSetup,
  needsStudioSetup,
  SETUP_STORAGE_KEY,
  verifyStudioSetup,
  studioSetupDestination,
} from '#/lib/connection/onboarding'
import { PROFILES_STORAGE_KEY } from '#/lib/connection/profiles'
import { createClient } from '#/gen/ov-client/client'
import type * as ClientModule from '#/gen/ov-client/client'

const get = vi.hoisted(() => vi.fn())
vi.mock('#/gen/ov-client/client', async (original) => {
  const actual = await original<typeof ClientModule>()
  return {
    ...actual,
    createClient: vi.fn(
      (options?: Parameters<typeof actual.createClient>[0]) => ({
        ...actual.createClient(options),
        get,
      }),
    ),
  }
})
const draft = {
  serviceSelection: 'opensource' as const,
  baseUrl: 'http://service.test',
  apiKey: 'synthetic-key',
  adminApiKey: '',
  accountId: 'default',
  userId: 'default',
}
beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  get.mockReset()
  vi.mocked(createClient).mockClear()
})
describe('first-use detection', () => {
  it('starts setup for an empty browser and retains unfinished setup on reload', () => {
    expect(needsStudioSetup()).toBe(true)
    localStorage.setItem(SETUP_STORAGE_KEY, 'pending')
    localStorage.setItem(
      PROFILES_STORAGE_KEY,
      JSON.stringify({ profiles: [{ connection: draft }] }),
    )
    expect(needsStudioSetup()).toBe(true)
  })
  it('keeps existing profile and legacy users out of the wizard', () => {
    localStorage.setItem('ov_console_connection', JSON.stringify(draft))
    expect(needsStudioSetup()).toBe(false)
    localStorage.clear()
    localStorage.setItem(
      PROFILES_STORAGE_KEY,
      JSON.stringify({ profiles: [{ connection: draft }] }),
    )
    expect(needsStudioSetup()).toBe(false)
  })
  it('does not treat an automatically persisted empty same-origin connection as configured', () => {
    localStorage.setItem(
      'ov_console_connection',
      JSON.stringify({ ...draft, baseUrl: window.location.origin, apiKey: '' }),
    )
    expect(needsStudioSetup()).toBe(true)
  })
  it('completes a verified development connection without a key', () => {
    completeStudioSetup(
      { ...draft, baseUrl: window.location.origin, apiKey: '' },
      'Development',
    )
    expect(needsStudioSetup()).toBe(false)
    localStorage.removeItem(PROFILES_STORAGE_KEY)
    localStorage.removeItem('ov_console_connection')
    expect(needsStudioSetup()).toBe(true)
  })
})
describe('verification', () => {
  it('uses hosted Bearer credentials and service-returned identity before checking private data', async () => {
    get
      .mockResolvedValueOnce({
        data: {
          status: 'ok',
          version: 'cloud',
          auth_mode: 'api_key',
          account_id: 'tenant',
          user_id: 'alice',
        },
      })
      .mockResolvedValueOnce({
        status: 200,
        headers: {},
        data: { status: 'ok', result: [] },
      })
    const c = await verifyStudioSetup(
      {
        ...draft,
        serviceSelection: 'volcengine',
        adminApiKey: 'never-send-control',
      },
      new AbortController().signal,
    )
    expect(c.userId).toBe('alice')
    expect(c.adminApiKey).toBe('')
    expect(vi.mocked(createClient).mock.calls[0][0]?.headers).toEqual({
      Authorization: 'Bearer synthetic-key',
    })
    expect(get.mock.calls[1][0].query.uri).toBe('viking://~/')
  })
  it('does not invent a hosted identity when health omits it', async () => {
    get
      .mockResolvedValueOnce({
        data: { version: 'test', auth_mode: 'api_key' },
      })
      .mockResolvedValueOnce({
        status: 200,
        headers: {},
        data: { status: 'ok', result: [] },
      })
    const connection = await verifyStudioSetup(
      { ...draft, serviceSelection: 'volcengine' },
      new AbortController().signal,
    )
    expect(connection.accountId).toBe('')
    expect(connection.userId).toBe('')
  })
  it('rejects HTML health responses instead of accepting the Studio URL', async () => {
    get.mockResolvedValueOnce({ data: '<html></html>' })
    await expect(
      verifyStudioSetup(draft, new AbortController().signal),
    ).rejects.toThrow('invalidService')
    expect(get).toHaveBeenCalledTimes(1)
  })
  it('does not accept a health-only response when data access fails', async () => {
    get
      .mockResolvedValueOnce({
        data: { version: 'test', auth_mode: 'api_key' },
      })
      .mockRejectedValueOnce(new Error('Forbidden'))
    await expect(
      verifyStudioSetup(draft, new AbortController().signal),
    ).rejects.toThrow('Forbidden')
    expect(localStorage.getItem(PROFILES_STORAGE_KEY)).toBeNull()
  })
  it('requires a key except in development mode', async () => {
    get.mockResolvedValueOnce({
      data: { version: 'test', auth_mode: 'api_key' },
    })
    await expect(
      verifyStudioSetup({ ...draft, apiKey: '' }, new AbortController().signal),
    ).rejects.toThrow('keyRequired')
  })
})

describe('management bootstrap', () => {
  it.each(['root', 'admin'])(
    'verifies %s control access without calling tenant data APIs',
    async (role) => {
      get
        .mockResolvedValueOnce({
          data: {
            version: 'test',
            auth_mode: 'api_key',
            role,
            account_id: 'tenant',
            user_id: 'admin',
          },
        })
        .mockResolvedValueOnce({
          status: 200,
          headers: {},
          data: { status: 'ok', result: [] },
        })
      const connection = await verifyStudioSetup(
        { ...draft, apiKey: '', adminApiKey: 'synthetic-control' },
        new AbortController().signal,
      )
      expect(get.mock.calls[1][0].url).toBe(
        role === 'root'
          ? '/api/v1/admin/accounts'
          : '/api/v1/admin/accounts/{account_id}/users',
      )
      expect(studioSetupDestination(connection)).toBe('/users')
      expect(
        get.mock.calls.some(([options]) => options.url === '/api/v1/fs/ls'),
      ).toBe(false)
    },
  )
  it('rejects denied management access and does not save it', async () => {
    get
      .mockResolvedValueOnce({
        data: { version: 'test', auth_mode: 'api_key', role: 'root' },
      })
      .mockRejectedValueOnce(new Error('Forbidden'))
    await expect(
      verifyStudioSetup(
        { ...draft, apiKey: '', adminApiKey: 'synthetic-control' },
        new AbortController().signal,
      ),
    ).rejects.toThrow('Forbidden')
    expect(localStorage.getItem(PROFILES_STORAGE_KEY)).toBeNull()
  })
  it('does not use a hosted control credential as a user credential', async () => {
    get.mockResolvedValueOnce({
      data: { version: 'test', auth_mode: 'api_key', role: 'root' },
    })
    await expect(
      verifyStudioSetup(
        {
          ...draft,
          serviceSelection: 'volcengine',
          apiKey: '',
          adminApiKey: 'synthetic-control',
        },
        new AbortController().signal,
      ),
    ).rejects.toThrow('keyRequired')
    expect(get).toHaveBeenCalledTimes(1)
  })
  it('keeps data connections on their normal landing page', () => {
    expect(studioSetupDestination(draft)).toBe('/home')
    expect(
      studioSetupDestination({ ...draft, serviceSelection: 'volcengine' }),
    ).toBe('/filesystem')
  })
})
