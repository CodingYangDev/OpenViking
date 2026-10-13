import { useState } from 'react'
import { ClipboardIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '#/components/ui/button'
import { Tabs, TabsList, TabsTrigger } from '#/components/ui/tabs'
import { copyTextToClipboard } from '#/lib/clipboard'
import {
  useInvalidateVikingFs,
  useVikingFsList,
} from '#/routes/resources/-hooks/viking-fm'
import { LazyFilePreview } from '#/routes/resources/-components/lazy-file-preview'
import { FindPalette } from '#/routes/resources/-components/find-palette'
import { normalizeDirUri, parentUri } from '#/routes/resources/-lib/normalize'
import type { VikingFsEntry } from '#/routes/resources/-types/viking-fm'
import type { FilesystemSearch } from '../-lib/types'
import {
  createEntryFromUri,
  getAncestorUris,
  isDirectoryLevelFile,
} from '../-lib/utils'
import { ContextExplorerHeader, ContextTree } from './context-explorer'

/** The same tree and preview as the workbench, without its local Bot and write tools. */
export function HostedFilesystem({ search }: { search: FilesystemSearch }) {
  const { t } = useTranslation(['filesystem', 'studio'])
  const initialUri = normalizeDirUri(
    search.file ? parentUri(search.file) : search.uri || 'viking://~/',
  )
  const [scope, setScope] = useState(
    initialUri.startsWith('viking://resources') ? 'shared' : 'personal',
  )
  const rootUri = scope === 'shared' ? 'viking://resources/' : 'viking://~/'
  const [currentUri, setCurrentUri] = useState(initialUri)
  const [expandedKeys, setExpandedKeys] = useState(
    () => new Set(getAncestorUris(initialUri)),
  )
  const [selectedFile, setSelectedFile] = useState<VikingFsEntry | null>(() =>
    search.file && !isDirectoryLevelFile(search.file)
      ? createEntryFromUri(search.file, false)
      : createEntryFromUri(initialUri, true),
  )
  const [findOpen, setFindOpen] = useState(false)
  const query = useVikingFsList(currentUri, { output: 'agent', nodeLimit: 500 })
  const { invalidateList } = useInvalidateVikingFs()
  function select(entry: VikingFsEntry) {
    setSelectedFile(entry)
    if (entry.isDir) setCurrentUri(normalizeDirUri(entry.uri))
  }
  function navigate(uri: string) {
    const directory = uri.endsWith('/') || isDirectoryLevelFile(uri)
    const target = directory && isDirectoryLevelFile(uri) ? parentUri(uri) : uri
    const parent = normalizeDirUri(directory ? target : parentUri(target))
    setScope(parent.startsWith('viking://resources') ? 'shared' : 'personal')
    setCurrentUri(parent)
    setExpandedKeys(new Set([...expandedKeys, ...getAncestorUris(parent)]))
    setSelectedFile(createEntryFromUri(target, directory))
  }
  function changeScope(next: string) {
    setScope(next)
    const uri = next === 'shared' ? 'viking://resources/' : 'viking://~/'
    setCurrentUri(uri)
    setExpandedKeys(new Set())
    setSelectedFile(createEntryFromUri(uri, true))
  }
  return (
    <div className="-mx-4 -my-6 flex h-[calc(100svh-3rem)] min-h-0 flex-col bg-background md:-mx-6">
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <aside className="flex min-h-[180px] min-w-0 shrink-0 basis-[36%] flex-col border-b bg-muted/20 lg:min-h-0 lg:w-[300px] lg:basis-auto lg:border-b-0 lg:border-r">
          <ContextExplorerHeader
            activeTaskCount={0}
            hasActiveTasks={false}
            hasTasks={false}
            isRefreshing={query.isFetching}
            isRefreshingTasks={false}
            onOpenProcessingTasks={() => {}}
            onOpenSearch={() => setFindOpen(true)}
            onRefresh={() => void invalidateList()}
          />
          <Tabs
            value={scope}
            onValueChange={changeScope}
            className="shrink-0 border-b px-3"
          >
            <TabsList variant="line">
              <TabsTrigger value="personal">{t('studio:personal')}</TabsTrigger>
              <TabsTrigger value="shared">{t('studio:shared')}</TabsTrigger>
            </TabsList>
          </Tabs>
          <div className="min-h-0 flex-1">
            <ContextTree
              rootUri={rootUri}
              currentUri={currentUri}
              expandedKeys={expandedKeys}
              onExpandedKeysChange={setExpandedKeys}
              selectedFileUri={
                selectedFile && !selectedFile.isDir ? selectedFile.uri : null
              }
              onSelectDirectory={select}
              onSelectFile={select}
            />
          </div>
        </aside>
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="flex min-h-14 items-center gap-3 border-b px-4">
            <span
              className="min-w-0 flex-1 truncate px-1.5 py-1 font-mono text-xs font-semibold"
              title={selectedFile?.uri || currentUri}
            >
              {selectedFile?.uri || currentUri}
            </span>
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              title={t('copyUri')}
              onClick={() =>
                void copyTextToClipboard(selectedFile?.uri || currentUri)
              }
            >
              <ClipboardIcon className="size-4" />
            </Button>
          </div>
          <div className="min-h-0 flex-1">
            <LazyFilePreview
              readOnly
              file={selectedFile}
              hideDirectoryHeader
              showCloseButton={false}
              onClose={() => setSelectedFile(null)}
              onNavigate={navigate}
            />
          </div>
        </main>
      </div>
      <FindPalette
        open={findOpen}
        onClose={() => setFindOpen(false)}
        onNavigate={navigate}
        onNavigateDir={navigate}
        scopeUri={currentUri}
      />
    </div>
  )
}
