import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useTheme } from 'next-themes'
import {
  ArrowRightIcon,
  ArrowLeftIcon,
  CheckIcon,
  CloudIcon,
  ServerIcon,
  Loader2Icon,
} from 'lucide-react'
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
  studioSetupDestination,
  CLOUD_SETUP_URL,
  completeStudioSetup,
  markStudioSetupPending,
  verifyStudioSetup,
} from '#/lib/connection/onboarding'
import { isValidConnectionUrl } from '#/lib/connection/profiles'
import type { ConnectionDraft } from '#/hooks/use-app-connection'

const LANGUAGES = [
  ['zh-CN', '中文'],
  ['en', 'English'],
] as const
const PROVIDERS = [
  ['opensource', ServerIcon],
  ['volcengine', CloudIcon],
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
  const providerDraftsRef = useRef<
    Partial<Record<(typeof PROVIDERS)[number][0], ConnectionDraft>>
  >({})
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
  function switchProvider(provider: (typeof PROVIDERS)[number][0]) {
    const current =
      draft.serviceSelection === 'volcengine' ? 'volcengine' : 'opensource'
    if (provider === current) return
    providerDraftsRef.current[current] = draft
    setDraft(
      providerDraftsRef.current[provider] ?? {
        serviceSelection: provider,
        baseUrl: provider === 'volcengine' ? CLOUD_SETUP_URL : '',
        apiKey: '',
        adminApiKey: '',
        accountId: 'default',
        userId: 'default',
      },
    )
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
    <main className="flex h-svh flex-col items-center overflow-y-auto bg-background px-5 py-6 text-foreground sm:px-8 sm:py-16">
      <div className="my-auto w-full max-w-5xl shrink-0">
        <div className="mb-6 flex items-center gap-3 lg:mb-14">
          <img src="/ov-logo.png" alt="" className="size-10 rounded-xl" />
          <span className="text-base font-semibold tracking-tight">
            {t('brand')}
          </span>
        </div>
        <div className="grid items-start gap-6 lg:grid-cols-[1fr_1.15fr] lg:gap-20">
          <header className="lg:pt-6">
            <p className="mb-4 text-xs font-medium tracking-widest text-muted-foreground">
              {t('steps')}
            </p>
            <h1
              ref={headingRef}
              tabIndex={-1}
              className="max-w-md whitespace-pre-line text-3xl leading-tight font-semibold tracking-tight outline-none sm:text-4xl lg:text-[42px] lg:leading-[1.2]"
            >
              {step === 0 ? t('welcome') : t('connectionTitle')}
            </h1>
            <p className="mt-3 max-w-sm text-sm leading-6 lg:mt-5 lg:leading-7 text-muted-foreground">
              {step === 0
                ? t('welcomeDescription')
                : t('connectionDescription')}
            </p>
            <ol
              className="mt-5 flex gap-6 lg:mt-12 lg:flex-col lg:gap-7"
              aria-label={t('steps')}
            >
              {[t('preferences'), t('connection')].map((label, index) => (
                <li
                  key={index}
                  aria-current={step === index ? 'step' : undefined}
                  className="flex items-start gap-3"
                >
                  <span
                    aria-hidden="true"
                    className={`flex size-7 shrink-0 items-center justify-center rounded-full border text-xs font-medium ${step >= index ? 'border-foreground bg-foreground text-background' : 'border-border text-muted-foreground'}`}
                  >
                    {step > index ? (
                      <CheckIcon className="size-3.5" />
                    ) : (
                      index + 1
                    )}
                  </span>
                  <div className="pt-1">
                    <p
                      className={`text-sm font-medium ${step === index ? 'text-foreground' : 'text-muted-foreground'}`}
                    >
                      {label}
                    </p>
                    <p className="mt-1 hidden text-xs leading-5 text-muted-foreground lg:block">
                      {t(index === 0 ? 'preferencesHint' : 'connectionHint')}
                    </p>
                  </div>
                </li>
              ))}
            </ol>
          </header>
          <Card className="gap-0 overflow-hidden rounded-2xl border-border/70 bg-card py-0 shadow-sm">
            <CardHeader className="gap-2 border-b border-border/70 px-6 py-4 sm:px-8 sm:py-6">
              <CardTitle className="text-lg font-semibold tracking-tight">
                {step === 0 ? t('preferences') : t('connection')}
              </CardTitle>
              <CardDescription className="text-sm leading-6">
                {t(step === 0 ? 'preferencesHint' : 'connectionHint')}
              </CardDescription>
            </CardHeader>
            <CardContent className="px-6 py-5 sm:px-8 sm:py-7">
              {step === 0 ? (
                <div className="space-y-5 sm:space-y-6">
                  <fieldset className="space-y-3">
                    <legend className="text-sm font-medium">
                      {t('language')}
                    </legend>
                    <div className="grid grid-cols-2 gap-3">
                      {LANGUAGES.map(([value, label]) => (
                        <label
                          key={value}
                          className="flex min-h-14 cursor-pointer items-center justify-between gap-3 rounded-lg border border-border px-4 transition-colors hover:bg-muted/40 has-checked:border-foreground has-checked:bg-muted/40 has-focus-visible:ring-2 has-focus-visible:ring-ring has-focus-visible:ring-offset-2"
                        >
                          <input
                            className="sr-only"
                            type="radio"
                            name="language"
                            value={value}
                            checked={language === value}
                            onChange={() => {
                              void i18n.changeLanguage(value)
                              document.documentElement.lang = value
                            }}
                          />
                          <span className="text-sm font-medium">{label}</span>
                          <span
                            aria-hidden="true"
                            className={`flex size-4 items-center justify-center rounded-full border ${language === value ? 'border-foreground bg-foreground text-background' : 'border-muted-foreground/30'}`}
                          >
                            {language === value ? (
                              <CheckIcon className="size-3" />
                            ) : null}
                          </span>
                        </label>
                      ))}
                    </div>
                  </fieldset>
                  <fieldset className="space-y-3">
                    <legend className="text-sm font-medium">
                      {t('theme')}
                    </legend>
                    <div className="grid grid-cols-3 gap-3">
                      {THEMES.map((value) => (
                        <label
                          key={value}
                          className="min-w-0 cursor-pointer rounded-xl border border-border p-2.5 transition-colors hover:bg-muted/40 has-checked:border-foreground has-checked:bg-muted/30 has-focus-visible:ring-2 has-focus-visible:ring-ring has-focus-visible:ring-offset-2"
                        >
                          <input
                            className="sr-only"
                            type="radio"
                            name="theme"
                            value={value}
                            checked={(theme ?? 'system') === value}
                            onChange={() => setTheme(value)}
                          />
                          <ThemePreview appearance={value} />
                          <span className="mt-3 flex items-center justify-between gap-1 text-xs font-medium sm:text-sm">
                            {t(value)}
                            {(theme ?? 'system') === value ? (
                              <CheckIcon
                                aria-hidden="true"
                                className="size-3.5 shrink-0"
                              />
                            ) : null}
                          </span>
                        </label>
                      ))}
                    </div>
                  </fieldset>
                  <Button
                    className="mt-2 min-h-11 w-full rounded-lg"
                    onClick={() => setStep(1)}
                  >
                    {t('next')}
                    <ArrowRightIcon aria-hidden="true" className="size-4" />
                  </Button>
                </div>
              ) : (
                <form
                  onSubmit={(event) => void submit(event)}
                  className="space-y-5"
                >
                  <fieldset disabled={busy} className="space-y-4">
                    <fieldset className="space-y-3">
                      <legend className="text-sm font-medium">
                        {t('studio:serviceType')}
                      </legend>
                      <div className="grid grid-cols-2 gap-3">
                        {PROVIDERS.map(([provider, Icon]) => (
                          <label
                            key={provider}
                            className="flex min-h-14 cursor-pointer items-center gap-2.5 rounded-lg border border-border px-3 text-sm font-medium transition-colors hover:bg-muted/40 has-checked:border-foreground has-checked:bg-muted/40 has-focus-visible:ring-2 has-focus-visible:ring-ring"
                          >
                            <input
                              className="sr-only"
                              type="radio"
                              name="provider"
                              value={provider}
                              checked={draft.serviceSelection === provider}
                              onChange={() => switchProvider(provider)}
                            />
                            <Icon
                              aria-hidden="true"
                              className="size-4 shrink-0 text-muted-foreground"
                            />
                            {t(`studio:providers.${provider}`)}
                          </label>
                        ))}
                      </div>
                    </fieldset>
                    <label
                      className="grid gap-2 text-sm font-medium"
                      htmlFor="setup-url"
                    >
                      {t('settings:fields.baseUrl')}
                      <Input
                        className="h-11 rounded-lg bg-background/60 text-sm"
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
                        className="h-11 rounded-lg bg-background/60 text-sm"
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
                        <summary className="cursor-pointer text-xs font-medium text-muted-foreground transition-colors hover:text-foreground">
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
                                type={
                                  key === 'adminApiKey' ? 'password' : 'text'
                                }
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
                    <p
                      role="alert"
                      className="rounded-lg border border-destructive/20 bg-destructive/5 p-3 text-sm leading-6 text-destructive"
                    >
                      {t('verifyFailed')} {error}
                    </p>
                  ) : null}
                  {verified ? (
                    <p
                      role="status"
                      className="rounded-lg border border-emerald-600/20 bg-emerald-600/5 p-3 text-sm leading-6 text-emerald-700 dark:text-emerald-400"
                    >
                      {t(
                        studioSetupDestination(verified) === '/users'
                          ? 'verifiedManagement'
                          : 'verified',
                      )}
                      {verified.accountId && verified.userId
                        ? ` ${verified.accountId} / ${verified.userId}`
                        : null}
                    </p>
                  ) : null}
                  <div className="flex gap-3 border-t border-border/70 pt-5">
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-11 rounded-lg"
                      onClick={() => {
                        abortRef.current?.abort()
                        setBusy(false)
                        setStep(0)
                      }}
                    >
                      <ArrowLeftIcon aria-hidden="true" className="size-4" />
                      {t('back')}
                    </Button>
                    <Button
                      type="submit"
                      className="min-h-11 flex-1 rounded-lg"
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
      </div>
    </main>
  )
}

function ThemePreview({ appearance }: { appearance: (typeof THEMES)[number] }) {
  return (
    <div
      aria-hidden="true"
      className={`relative h-16 overflow-hidden rounded-md border border-black/10 sm:h-20 ${appearance === 'light' ? 'bg-[#f9fafb]' : appearance === 'dark' ? 'bg-[#191b20]' : 'bg-[linear-gradient(110deg,#f9fafb_50%,#191b20_50%)]'}`}
    >
      <div
        className={`flex h-4 items-center gap-1 border-b px-2 ${appearance === 'dark' ? 'border-white/10' : 'border-black/10'}`}
      >
        {[0, 1, 2].map((dot) => (
          <span
            key={dot}
            className={`size-1 rounded-full ${appearance === 'dark' ? 'bg-white/25' : 'bg-black/20'}`}
          />
        ))}
      </div>
      <div className="flex h-full">
        <div
          className={`w-1/4 border-r p-1.5 ${appearance === 'dark' ? 'border-white/10 bg-white/5' : 'border-black/10 bg-black/[0.03]'}`}
        >
          <div
            className={`mb-1.5 h-1 rounded-full ${appearance === 'dark' ? 'bg-white/20' : 'bg-black/15'}`}
          />
          <div
            className={`h-1 w-2/3 rounded-full ${appearance === 'dark' ? 'bg-white/10' : 'bg-black/10'}`}
          />
        </div>
        <div className="flex-1 space-y-1.5 p-2">
          <div
            className={`h-1.5 w-3/5 rounded-full ${appearance === 'dark' ? 'bg-white/35' : 'bg-black/20'}`}
          />
          <div
            className={`h-1 w-full rounded-full ${appearance === 'dark' ? 'bg-white/10' : 'bg-black/10'}`}
          />
          <div
            className={`h-1 w-4/5 rounded-full ${appearance === 'dark' ? 'bg-white/10' : 'bg-black/10'}`}
          />
        </div>
      </div>
    </div>
  )
}
