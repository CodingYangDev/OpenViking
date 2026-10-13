// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { HostedFilesystem } from './hosted-filesystem'

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  invalidate: vi.fn(),
  preview: vi.fn(),
}))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))
vi.mock('#/routes/resources/-hooks/viking-fm', () => ({
  useVikingFsList: mocks.list,
  useInvalidateVikingFs: () => ({ invalidateList: mocks.invalidate }),
}))
vi.mock('#/routes/resources/-components/lazy-file-preview', () => ({
  LazyFilePreview: (props: { readOnly: boolean; file?: { uri: string } }) => {
    mocks.preview(props)
    return <div data-testid="preview">{props.file?.uri}</div>
  },
}))
vi.mock('#/routes/resources/-components/find-palette', () => ({
  FindPalette: () => null,
}))
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})
it('uses the existing tree and preview without upload or local Agent tools, and changes directory scope through tabs', () => {
  mocks.list.mockReturnValue({ data: { entries: [] }, isFetching: false })
  render(<HostedFilesystem search={{ upload: false }} />)
  expect(mocks.list.mock.calls.every(([uri]) => uri === 'viking://~/')).toBe(
    true,
  )
  expect(screen.getByTestId('preview').textContent).toBe('viking://~/')
  expect(mocks.preview.mock.lastCall?.[0].readOnly).toBe(true)
  expect(screen.queryByTitle('explorer.addResource')).toBeNull()
  expect(screen.queryByText('tabs.agent')).toBeNull()
  expect(screen.queryByText('tabs.terminal')).toBeNull()
  fireEvent.click(screen.getByRole('tab', { name: 'studio:shared' }))
  expect(screen.getByTestId('preview').textContent).toBe('viking://resources/')
  expect(mocks.list.mock.lastCall?.[0]).toBe('viking://resources/')
})
it('opens a linked file directly in the read-only preview', () => {
  mocks.list.mockReturnValue({ data: { entries: [] }, isFetching: false })
  render(
    <HostedFilesystem
      search={{ file: 'viking://resources/guide.md', upload: false }}
    />,
  )
  expect(screen.getByTestId('preview').textContent).toBe(
    'viking://resources/guide.md',
  )
  expect(mocks.preview.mock.lastCall?.[0].readOnly).toBe(true)
})
