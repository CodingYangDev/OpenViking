// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AppShell, ConnectionScopedRouteContent } from './app-shell'

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
