// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StudioSetup } from './studio-setup'

const mocks = vi.hoisted(() => ({
  verify: vi.fn(),
  complete: vi.fn(),
  language: vi.fn(),
  theme: vi.fn(),
}))
vi.mock('next-themes', () => ({
  useTheme: () => ({ theme: 'system', setTheme: mocks.theme }),
}))
vi.mock('react-i18next', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: {
      language: 'en',
      resolvedLanguage: 'en',
      changeLanguage: mocks.language,
    },
  }),
}))
vi.mock('#/lib/connection/onboarding', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  markStudioSetupPending: vi.fn(),
  verifyStudioSetup: mocks.verify,
  completeStudioSetup: mocks.complete,
}))
afterEach(cleanup)
beforeEach(() => vi.clearAllMocks())
it('combines preferences, applies changes immediately and retains connection fields when returning', () => {
  render(<StudioSetup onComplete={vi.fn()} />)
  fireEvent.click(screen.getByLabelText('中文'))
  expect(mocks.language).toHaveBeenCalledWith('zh-CN')
  fireEvent.click(screen.getByLabelText('dark'))
  expect(mocks.theme).toHaveBeenCalledWith('dark')
  fireEvent.click(screen.getByRole('button', { name: 'next' }))
  fireEvent.change(screen.getByLabelText('settings:fields.baseUrl'), {
    target: { value: 'http://service.test' },
  })
  fireEvent.change(screen.getByLabelText('settings:fields.userApiKey'), {
    target: { value: 'synthetic-key' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'back' }))
  fireEvent.click(screen.getByRole('button', { name: 'next' }))
  expect(
    screen.getByLabelText<HTMLInputElement>('settings:fields.baseUrl').value,
  ).toBe('http://service.test')
  expect(
    screen.getByLabelText<HTMLInputElement>('settings:fields.userApiKey').value,
  ).toBe('synthetic-key')
})
it('enters only after verification, invalidates verification when fields change, and persists on explicit entry', async () => {
  const done = vi.fn()
  mocks.verify.mockImplementation(async (draft) => draft)
  render(<StudioSetup onComplete={done} />)
  fireEvent.click(screen.getByRole('button', { name: 'next' }))
  fireEvent.change(screen.getByLabelText('settings:fields.baseUrl'), {
    target: { value: 'http://service.test' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'verify' }))
  await screen.findByRole('status')
  expect(done).not.toHaveBeenCalled()
  expect(mocks.complete).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('settings:fields.userApiKey'), {
    target: { value: 'changed' },
  })
  expect(screen.queryByRole('status')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'verify' }))
  await screen.findByRole('status')
  fireEvent.click(screen.getByRole('button', { name: 'enter' }))
  expect(mocks.complete).toHaveBeenCalledOnce()
  expect(done).toHaveBeenCalledOnce()
})
it('keeps the user in setup on verification failure', async () => {
  mocks.verify.mockRejectedValueOnce(new Error('Forbidden'))
  const done = vi.fn()
  render(<StudioSetup onComplete={done} />)
  fireEvent.click(screen.getByRole('button', { name: 'next' }))
  fireEvent.change(screen.getByLabelText('settings:fields.baseUrl'), {
    target: { value: 'http://service.test' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'verify' }))
  await waitFor(() =>
    expect(screen.getByRole('alert').textContent).toContain('Forbidden'),
  )
  expect(done).not.toHaveBeenCalled()
})

it('isolates provider addresses and credentials, restores drafts, and invalidates verification', async () => {
  mocks.verify.mockImplementation(async (draft) => draft)
  render(<StudioSetup onComplete={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'next' }))
  const url = () =>
    screen.getByLabelText<HTMLInputElement>('settings:fields.baseUrl')
  const key = () =>
    screen.getByLabelText<HTMLInputElement>('settings:fields.userApiKey')
  fireEvent.change(url(), { target: { value: 'http://oss.test' } })
  fireEvent.change(key(), { target: { value: 'synthetic-oss' } })
  fireEvent.change(screen.getByLabelText('settings:fields.rootApiKey'), {
    target: { value: 'synthetic-control' },
  })
  fireEvent.click(screen.getByLabelText('studio:providers.volcengine'))
  expect(url().value).toBe(
    'https://api.vikingdb.cn-beijing.volces.com/openviking',
  )
  expect(key().value).toBe('')
  fireEvent.change(url(), {
    target: { value: 'https://cloud-proxy.test/openviking' },
  })
  fireEvent.change(key(), { target: { value: 'synthetic-cloud' } })
  fireEvent.click(screen.getByRole('button', { name: 'verify' }))
  await screen.findByRole('status')
  fireEvent.click(screen.getByLabelText('studio:providers.opensource'))
  expect(screen.queryByRole('status')).toBeNull()
  expect(url().value).toBe('http://oss.test')
  expect(key().value).toBe('synthetic-oss')
  expect(
    screen.getByLabelText<HTMLInputElement>('settings:fields.rootApiKey').value,
  ).toBe('synthetic-control')
  fireEvent.click(screen.getByLabelText('studio:providers.volcengine'))
  expect(url().value).toBe('https://cloud-proxy.test/openviking')
  expect(key().value).toBe('synthetic-cloud')
  expect(screen.queryByRole('status')).toBeNull()
})
it('does not carry the default hosted URL back to an untouched open-source tab', () => {
  render(<StudioSetup onComplete={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'next' }))
  fireEvent.click(screen.getByLabelText('studio:providers.volcengine'))
  fireEvent.click(screen.getByLabelText('studio:providers.opensource'))
  expect(
    screen.getByLabelText<HTMLInputElement>('settings:fields.baseUrl').value,
  ).toBe('')
})
