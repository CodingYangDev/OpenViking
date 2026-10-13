// @vitest-environment jsdom
import type { ComponentType } from 'react'
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'

import { Route } from './route'

const { probe, reconnect, serviceState, config, connectionState } = vi.hoisted(
  () => ({
    connectionState: {
      baseUrl: '',
      serviceSelection: 'auto',
      accountId: 'account-a',
      userId: 'alice',
      adminApiKey: '',
      apiKey: '',
    },
    probe: vi.fn(),
    reconnect: vi.fn(),
    serviceState: {
      provider: 'opensource',
      ready: true,
      role: 'user',
      key: '',
    },
    config: { url: 'http://localhost:1933', selection: 'auto' },
  }),
)
vi.mock('#/lib/admin', () => ({ probeStudioConnection: probe }))
vi.mock('#/hooks/use-app-connection', () => ({
  useAppConnection: () => ({
    connection: Object.assign(connectionState, {
      baseUrl: config.url,
      serviceSelection: config.selection,
      adminApiKey: serviceState.key,
    }),
    serverMode: 'trusted',
    connectionRole: serviceState.role,
    isConnectionRoleLoading: false,
    identityScopeKey: 'settings-test',
    saveConnection: vi.fn(),
    reconnect,
  }),
}))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { resolvedLanguage: 'en' },
  }),
}))
vi.mock('#/hooks/use-studio-service', () => ({
  useStudioService: () => ({
    provider: serviceState.provider,
    ready: serviceState.ready,
    version: '',
    isChecking: false,
    health: { refetch: vi.fn() },
  }),
}))
vi.mock('./-components/server-configuration', () => ({
  ServerConfigurationEditor: () => <span data-testid="configuration-editor" />,
}))
afterEach(cleanup)

it.each([false, undefined, true])(
  'requires the Root key in trusted mode regardless of legacy metadata: %s',
  async (required) => {
    probe.mockResolvedValue({
      admin: {
        state: 'error',
        statusCode: 403,
        detail: 'Management unavailable',
      },
      data: { state: 'ok', detailCode: 'tenantDataAvailable' },
      rootApiKeyRequired: required,
    })
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const Settings = Route.options.component as ComponentType & {
      preload?: () => Promise<unknown>
    }
    await Settings.preload?.()
    render(
      <QueryClientProvider client={client}>
        <Settings />
      </QueryClientProvider>,
    )
    await waitFor(
      () => expect(screen.getByText('Management unavailable')).toBeTruthy(),
      { timeout: 5000 },
    )
    expect(
      Boolean(screen.queryByText('connection.keyGuide.trusted.title')),
    ).toBe(true)
    client.clear()
  },
)

it('keeps management credentials editable after authentication fails and retries the connection owner', async () => {
  serviceState.provider = 'unknown'
  config.url = 'https://custom.example'
  config.selection = 'opensource'
  probe.mockResolvedValue({
    admin: { state: 'skipped' },
    data: { state: 'skipped' },
  })
  const client = new QueryClient()
  const Settings = Route.options.component as ComponentType
  render(
    <QueryClientProvider client={client}>
      <Settings />
    </QueryClientProvider>,
  )
  expect(document.getElementById('settings-root-api-key')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'studio:retry' }))
  expect(reconnect).toHaveBeenCalledTimes(1)
  client.clear()
  serviceState.provider = 'opensource'
  config.url = 'http://localhost:1933'
  config.selection = 'auto'
})

it('unmounts the configuration editor and resets the active tab when switching to a hosted connection', async () => {
  Object.assign(serviceState, {
    provider: 'opensource',
    ready: true,
    role: 'root',
    key: 'root-key',
  })
  probe.mockResolvedValue({ admin: { state: 'ok' }, data: { state: 'ok' } })
  const client = new QueryClient()
  const Settings = Route.options.component as ComponentType
  const content = () => (
    <QueryClientProvider client={client}>
      <Settings />
    </QueryClientProvider>
  )
  const view = render(content())
  fireEvent.click(screen.getByRole('tab', { name: 'tabs.configuration' }))
  expect(await screen.findByTestId('configuration-editor')).toBeTruthy()
  serviceState.provider = 'volcengine'
  view.rerender(content())
  expect(screen.queryByRole('tab', { name: 'tabs.configuration' })).toBeNull()
  expect(screen.queryByTestId('configuration-editor')).toBeNull()
  expect(
    screen
      .getByRole('tab', { name: 'tabs.connection' })
      .getAttribute('aria-selected'),
  ).toBe('true')
  client.clear()
  Object.assign(serviceState, { provider: 'opensource', role: 'user', key: '' })
})
