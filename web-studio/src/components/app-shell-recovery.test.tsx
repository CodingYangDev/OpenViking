// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { IdentityScopedAppShell } from './app-shell'

const state = vi.hoisted(() => ({ pathname: '/filesystem' }))
vi.mock('#/hooks/use-app-connection', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  useAppConnection: () => ({
    identityScopeKey: 'test',
    serverMode: 'checking',
  }),
}))
vi.mock('@tanstack/react-router', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  Link: ({
    to,
    children,
    ...props
  }: {
    to: string
    children?: React.ReactNode
  }) => (
    <a href={to} {...props}>
      {children}
    </a>
  ),
  useRouterState: ({
    select,
  }: {
    select: (state: { location: { pathname: string } }) => unknown
  }) => select({ location: { pathname: state.pathname } }),
}))
afterEach(cleanup)
it('keeps settings reachable while a health request is pending', () => {
  state.pathname = '/filesystem'
  render(
    <IdentityScopedAppShell>
      <span data-testid="business-route" />
    </IdentityScopedAppShell>,
  )
  expect(screen.queryByTestId('business-route')).toBeNull()
  expect(
    screen
      .getByRole('button', { name: 'Connection settings' })
      .getAttribute('href'),
  ).toBe('/settings')
})
it('mounts the configuration screen without waiting for health', () => {
  state.pathname = '/settings'
  render(
    <IdentityScopedAppShell>
      <span data-testid="connection-settings" />
    </IdentityScopedAppShell>,
  )
  expect(screen.getByTestId('connection-settings')).toBeTruthy()
})
