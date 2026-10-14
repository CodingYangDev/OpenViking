// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  AppShell,
  ConnectionScopedRouteContent,
  shouldShowGatewayNavigation,
} from './app-shell'

const bootstrap = vi.hoisted(() => ({ health: vi.fn() }))
vi.mock('#/lib/connection/health', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  fetchServerHealth: bootstrap.health,
}))
vi.mock('#/hooks/use-app-connection', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  AppConnectionProvider: () => <span data-testid="connected-provider" />,
}))

vi.mock('@tanstack/react-router', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  useNavigate: () => vi.fn(),
}))

afterEach(cleanup)

describe('ConnectionScopedRouteContent', () => {
  it('does not mount route content while the server mode is unresolved', () => {
    render(
      <ConnectionScopedRouteContent serverMode="checking">
        <span data-testid="identity-scoped-route" />
      </ConnectionScopedRouteContent>,
    )

    expect(screen.queryByTestId('identity-scoped-route')).toBeNull()
  })

  it.each(['api_key', 'trusted', 'dev', 'oidc', 'ldap', 'offline'] as const)(
    'mounts route content after resolving %s mode',
    (serverMode) => {
      render(
        <ConnectionScopedRouteContent serverMode={serverMode}>
          <span data-testid="identity-scoped-route" />
        </ConnectionScopedRouteContent>,
      )

      expect(screen.getByTestId('identity-scoped-route')).toBeTruthy()
    },
  )
})

it('shows first-use setup before mounting business routes or the connection provider', () => {
  localStorage.clear()
  sessionStorage.clear()
  render(
    <AppShell>
      <span data-testid="business-route" />
    </AppShell>,
  )
  expect(screen.queryByTestId('business-route')).toBeNull()
  expect(screen.getByRole('heading', { level: 1 }).textContent).toContain(
    'Welcome',
  )
  expect(localStorage.getItem('ov_console_connection_profiles')).toBeNull()
})

describe('gateway navigation', () => {
  it.each([
    ['dev', true, true],
    ['api_key', true, true],
    ['trusted', true, true],
    ['api_key', false, false],
    ['offline', true, false],
    ['checking', true, false],
  ] as const)('%s allowed %s: %s', (serverMode, allowed, visible) => {
    expect(
      shouldShowGatewayNavigation({
        isGatewayHost: false,
        serverMode,
        allowed,
      }),
    ).toBe(visible)
  })

  it('always shows the entry on a standalone gateway', () => {
    expect(
      shouldShowGatewayNavigation({
        isGatewayHost: true,
        serverMode: 'offline',
        allowed: false,
      }),
    ).toBe(true)
  })
})

it('opens a fresh standalone Gateway Studio without the data-connection wizard', async () => {
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/studio/')
  bootstrap.health.mockResolvedValueOnce({
    service: 'openviking-gateway',
    auth_mode: 'api_key',
  })
  try {
    render(
      <AppShell>
        <span data-testid="business-route" />
      </AppShell>,
    )
    expect(await screen.findByTestId('connected-provider')).toBeTruthy()
    expect(bootstrap.health).toHaveBeenCalledWith(window.location.origin)
    expect(screen.queryByRole('heading')).toBeNull()
  } finally {
    window.history.replaceState(null, '', '/')
  }
})
