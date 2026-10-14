import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useTheme } from 'next-themes'
import { Loader2Icon } from 'lucide-react'
import { Button } from '#/components/ui/button'
import { Input } from '#/components/ui/input'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '#/components/ui/card'
import { PLAIN_INPUT_PROPS } from '#/lib/form-input'
import {
  StudioSetupError,
  CLOUD_SETUP_URL,
  completeStudioSetup,
  markStudioSetupPending,
  verifyStudioSetup,
} from '#/lib/studio-onboarding'
import { isValidConnectionUrl } from '#/lib/connection-profiles'
import type { ConnectionDraft } from '#/hooks/use-app-connection'

const LANGUAGES = [
  ['zh-CN', '中文'],
  ['en', 'English'],
] as const
const THEMES = ['light', 'dark', 'system'] as const
const ADVANCED_FIELDS = ['adminApiKey', 'accountId', 'userId'] as const

export function StudioSetup({
  onComplete,
}: {
  onComplete: (connection: ConnectionDraft) => void
}) {
  const { t, i18n } = useTranslation(['onboarding', 'studio', 'settings'])
  const { theme, setTheme } = useTheme()
  const [step, setStep] = useState(0)
  const [draft, setDraft] = useState<ConnectionDraft>({
    serviceSelection: 'opensource',
    baseUrl: '',
    apiKey: '',
    adminApiKey: '',
    accountId: 'default',
    userId: 'default',
  })
  const [verified, setVerified] = useState<ConnectionDraft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    markStudioSetupPending()
    return () => abortRef.current?.abort()
  }, [])
  useEffect(() => {
    headingRef.current?.focus()
  }, [step])
  const language = (i18n.resolvedLanguage ?? i18n.language).startsWith('zh')
    ? 'zh-CN'
    : 'en'
  function update(next: Partial<ConnectionDraft>) {
    setDraft((value) => ({ ...value, ...next }))
    setVerified(null)
    setError('')
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (busy) return
    setError('')
    if (verified) {
      try {
        completeStudioSetup(
          verified,
          t(`studio:providers.${verified.serviceSelection}`),
        )
        onComplete(verified)
      } catch {
        setError(t('storageError'))
      }
      return
    }
    if (!isValidConnectionUrl(draft.baseUrl)) {
      setError(t('studio:profiles.invalidUrl'))
      return
    }
    const controller = new AbortController()
    abortRef.current = controller
    setBusy(true)
    try {
      const result = await verifyStudioSetup(draft, controller.signal)
      if (!controller.signal.aborted) {
        setDraft(result)
        setVerified(result)
      }
    } catch (err) {
      if (!controller.signal.aborted)
        setError(
          err instanceof StudioSetupError
            ? t(err.code)
            : err instanceof Error
              ? err.message
              : String(err),
        )
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null
        setBusy(false)
      }
    }
  }
  return (
    <main className="flex min-h-svh items-center justify-center bg-background px-4 py-8">
      <div className="w-full max-w-xl space-y-6">
        <p className="text-center text-sm font-semibold tracking-wide">
          {t('brand')}
        </p>
        <ol
          className="flex justify-center gap-6 text-sm text-muted-foreground"
          aria-label={t('steps')}
        >
          {[t('preferences'), t('connection')].map((label, index) => (
            <li
              key={index}
              aria-current={step === index ? 'step' : undefined}
              className={step === index ? 'font-medium text-foreground' : ''}
            >
              {index + 1}. {label}
            </li>
          ))}
        </ol>
        <Card>
          <CardHeader>
            <CardTitle>
              <h1 ref={headingRef} tabIndex={-1}>
                {step === 0 ? t('welcome') : t('connection')}
              </h1>
            </CardTitle>
            <CardDescription>
              {step === 0
                ? t('welcomeDescription')
                : t('connectionDescription')}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {step === 0 ? (
              <div className="space-y-6">
                <fieldset className="space-y-3">
                  <legend className="text-sm font-medium">
                    {t('language')}
                  </legend>
                  <div className="grid grid-cols-2 gap-3">
                    {LANGUAGES.map(([value, label]) => (
                      <label
                        key={value}
                        className="flex min-h-12 cursor-pointer items-center gap-3 rounded-lg border p-3 has-checked:border-primary"
                      >
                        <input
                          type="radio"
                          name="language"
                          value={value}
                          checked={language === value}
                          onChange={() => {
                            void i18n.changeLanguage(value)
                            document.documentElement.lang = value
                          }}
                        />
                        {label}
                      </label>
                    ))}
                  </div>
                </fieldset>
                <fieldset className="space-y-3">
                  <legend className="text-sm font-medium">{t('theme')}</legend>
                  <div className="grid grid-cols-3 gap-3">
                    {THEMES.map((value) => (
                      <label
                        key={value}
                        className="flex min-h-12 cursor-pointer items-center gap-2 rounded-lg border p-3 text-sm has-checked:border-primary"
                      >
                        <input
                          type="radio"
                          name="theme"
                          value={value}
                          checked={(theme ?? 'system') === value}
                          onChange={() => setTheme(value)}
                        />
                        {t(value)}
                      </label>
                    ))}
                  </div>
                </fieldset>
                <Button className="min-h-12 w-full" onClick={() => setStep(1)}>
                  {t('next')}
                </Button>
              </div>
            ) : (
              <form
                onSubmit={(event) => void submit(event)}
                className="space-y-4"
              >
                <fieldset disabled={busy} className="space-y-4">
                  <label
                    className="grid gap-2 text-sm font-medium"
                    htmlFor="setup-provider"
                  >
                    {t('studio:serviceType')}
                    <select
                      id="setup-provider"
                      name="provider"
                      className="h-12 rounded-md border bg-background px-3 text-base"
                      value={draft.serviceSelection}
                      onChange={(event) => {
                        const provider = event.target.value as
                          | 'opensource'
                          | 'volcengine'
                        update({
                          serviceSelection: provider,
                          baseUrl:
                            provider === 'volcengine' && !draft.baseUrl
                              ? CLOUD_SETUP_URL
                              : draft.baseUrl,
                          adminApiKey:
                            provider === 'volcengine' ? '' : draft.adminApiKey,
                        })
                      }}
                    >
                      <option value="opensource">
                        {t('studio:providers.opensource')}
                      </option>
                      <option value="volcengine">
                        {t('studio:providers.volcengine')}
                      </option>
                    </select>
                  </label>
                  <label
                    className="grid gap-2 text-sm font-medium"
                    htmlFor="setup-url"
                  >
                    {t('settings:fields.baseUrl')}
                    <Input
                      className="h-12 text-base"
                      id="setup-url"
                      name="baseUrl"
                      required
                      type="url"
                      value={draft.baseUrl}
                      placeholder={
                        draft.serviceSelection === 'volcengine'
                          ? CLOUD_SETUP_URL
                          : 'http://127.0.0.1:1933'
                      }
                      onChange={(event) =>
                        update({ baseUrl: event.target.value })
                      }
                      {...PLAIN_INPUT_PROPS}
                    />
                  </label>
                  <label
                    className="grid gap-2 text-sm font-medium"
                    htmlFor="setup-key"
                  >
                    {t('settings:fields.userApiKey')}
                    <Input
                      className="h-12 text-base"
                      id="setup-key"
                      name="apiKey"
                      type="password"
                      value={draft.apiKey}
                      onChange={(event) =>
                        update({ apiKey: event.target.value })
                      }
                      {...PLAIN_INPUT_PROPS}
                    />
                  </label>
                  <p className="text-sm text-muted-foreground">
                    {t('keyHelp')}
                  </p>
                  {draft.serviceSelection === 'opensource' ? (
                    <details>
                      <summary className="cursor-pointer text-sm font-medium">
                        {t('advanced')}
                      </summary>
                      <div className="mt-3 grid gap-3">
                        {ADVANCED_FIELDS.map((key) => (
                          <label
                            className="grid gap-2 text-sm"
                            key={key}
                            htmlFor={`setup-${key}`}
                          >
                            {t(
                              `settings:fields.${key === 'accountId' ? 'account' : key === 'userId' ? 'user' : 'rootApiKey'}`,
                            )}
                            <Input
                              id={`setup-${key}`}
                              name={key}
                              type={key === 'adminApiKey' ? 'password' : 'text'}
                              value={draft[key]}
                              onChange={(event) =>
                                update({ [key]: event.target.value })
                              }
                              {...PLAIN_INPUT_PROPS}
                            />
                          </label>
                        ))}
                      </div>
                    </details>
                  ) : null}
                </fieldset>
                {error ? (
                  <p role="alert" className="text-sm text-destructive">
                    {t('verifyFailed')} {error}
                  </p>
                ) : null}
                {verified ? (
                  <p role="status" className="text-sm text-emerald-600">
                    {t('verified')}
                    {verified.accountId && verified.userId
                      ? ` ${verified.accountId} / ${verified.userId}`
                      : null}
                  </p>
                ) : null}
                <div className="flex gap-3">
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-12"
                    onClick={() => {
                      abortRef.current?.abort()
                      setBusy(false)
                      setStep(0)
                    }}
                  >
                    {t('back')}
                  </Button>
                  <Button
                    type="submit"
                    className="min-h-12 flex-1"
                    disabled={busy}
                  >
                    {busy ? (
                      <Loader2Icon className="size-4 animate-spin" />
                    ) : null}
                    {t(busy ? 'verifying' : verified ? 'enter' : 'verify')}
                  </Button>
                </div>
              </form>
            )}
          </CardContent>
        </Card>
      </div>
    </main>
  )
}
