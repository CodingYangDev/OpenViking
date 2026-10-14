import * as React from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '#/components/ui/button'
import { Badge } from '#/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '#/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '#/components/ui/dialog'
import { Input } from '#/components/ui/input'
import { Field, FieldLabel } from '#/components/ui/field'
import { useAppConnection } from '#/hooks/use-app-connection'
import { PLAIN_INPUT_PROPS } from '#/lib/form-input'
import type { ConnectionProfile } from '#/lib/connection/profiles'
import {
  isValidConnectionUrl,
  normalizeProfileConnection,
} from '#/lib/connection/profiles'

const PROFILE_FIELDS = [
  'baseUrl',
  'apiKey',
  'adminApiKey',
  'accountId',
  'userId',
] as const

export function ConnectionProfilesList() {
  const { t } = useTranslation(['studio', 'settings'])
  const {
    profiles = [],
    activeProfileId,
    saveProfile,
    activateProfile,
    deleteProfile,
  } = useAppConnection()
  const [draft, setDraft] = React.useState<ConnectionProfile | null>(null)
  const [error, setError] = React.useState('')
  const [deleting, setDeleting] = React.useState<ConnectionProfile | null>(null)
  const openEditor = (profile: ConnectionProfile) => {
    setError('')
    setDraft(profile)
  }
  function save(event: React.FormEvent) {
    event.preventDefault()
    if (!draft) return
    if (!isValidConnectionUrl(draft.connection.baseUrl)) {
      setError(t('profiles.invalidUrl'))
      return
    }
    const connection = normalizeProfileConnection(draft.connection)
    saveProfile({
      ...draft,
      connection: {
        ...connection,
        adminApiKey:
          connection.serviceSelection === 'volcengine'
            ? ''
            : connection.adminApiKey,
      },
    })
    setDraft(null)
  }
  return (
    <>
      <Card>
        <CardHeader className="flex flex-wrap items-center justify-between gap-3">
          <CardTitle>{t('profiles.title')}</CardTitle>
          <Button
            onClick={() =>
              openEditor({
                id: crypto.randomUUID(),
                name: '',
                connection: {
                  serviceSelection: 'opensource',
                  baseUrl: '',
                  apiKey: '',
                  adminApiKey: '',
                  accountId: 'default',
                  userId: 'default',
                },
              })
            }
          >
            {t('profiles.add')}
          </Button>
        </CardHeader>
        <CardContent className="grid gap-3">
          <p className="text-sm text-muted-foreground">{t('profiles.hint')}</p>
          {profiles.map((profile) => (
            <div
              key={profile.id}
              className="flex min-w-0 flex-col items-start justify-between gap-3 rounded-md border p-3 sm:flex-row sm:items-center"
            >
              <div className="w-full min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{profile.name}</span>
                  <Badge variant="outline">
                    {t(`providers.${profile.connection.serviceSelection}`)}
                  </Badge>
                  {profile.id === activeProfileId && (
                    <Badge>{t('profiles.active')}</Badge>
                  )}
                </div>
                <p className="mt-1 break-all text-sm text-muted-foreground">
                  {profile.connection.baseUrl}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  disabled={profile.id === activeProfileId}
                  onClick={() => activateProfile(profile.id)}
                >
                  {t('profiles.activate')}
                </Button>
                <Button variant="outline" onClick={() => openEditor(profile)}>
                  {t('profiles.edit')}
                </Button>
                <Button
                  variant="ghost"
                  disabled={profile.id === activeProfileId}
                  onClick={() => setDeleting(profile)}
                >
                  {t('profiles.delete')}
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
      <Dialog
        open={Boolean(draft)}
        onOpenChange={(open) => {
          if (!open) setDraft(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {t(
                profiles.some((p) => p.id === draft?.id)
                  ? 'profiles.edit'
                  : 'profiles.add',
              )}
            </DialogTitle>
            <DialogDescription>{t('profiles.saveHint')}</DialogDescription>
          </DialogHeader>
          {draft && (
            <form onSubmit={save} className="grid gap-4">
              <Field>
                <FieldLabel htmlFor="profile-name">
                  {t('profiles.name')}
                </FieldLabel>
                <Input
                  id="profile-name"
                  required
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  {...PLAIN_INPUT_PROPS}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="profile-provider">
                  {t('serviceType')}
                </FieldLabel>
                <select
                  id="profile-provider"
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={draft.connection.serviceSelection}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      connection: {
                        ...draft.connection,
                        serviceSelection: e.target.value as
                          | 'opensource'
                          | 'volcengine',
                      },
                    })
                  }
                >
                  <option value="opensource">
                    {t('providers.opensource')}
                  </option>
                  <option value="volcengine">
                    {t('providers.volcengine')}
                  </option>
                </select>
              </Field>
              {PROFILE_FIELDS.filter(
                (field) =>
                  draft.connection.serviceSelection === 'opensource' ||
                  field === 'baseUrl' ||
                  field === 'apiKey',
              ).map((key) => {
                return (
                  <Field key={key}>
                    <FieldLabel htmlFor={`profile-${key}`}>
                      {t(`profiles.fields.${key}`)}
                    </FieldLabel>
                    <Input
                      id={`profile-${key}`}
                      required={key === 'baseUrl'}
                      type={
                        key === 'apiKey' || key === 'adminApiKey'
                          ? 'password'
                          : 'text'
                      }
                      value={draft.connection[key]}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          connection: {
                            ...draft.connection,
                            [key]: e.target.value,
                          },
                        })
                      }
                      {...PLAIN_INPUT_PROPS}
                    />
                  </Field>
                )
              })}
              {error && (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}
              <div className="flex justify-end gap-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setDraft(null)}
                >
                  {t('profiles.cancel')}
                </Button>
                <Button type="submit" disabled={!draft.name.trim()}>
                  {t('profiles.save')}
                </Button>
              </div>
            </form>
          )}
        </DialogContent>
      </Dialog>
      <Dialog
        open={Boolean(deleting)}
        onOpenChange={(open) => {
          if (!open) setDeleting(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('profiles.delete')}</DialogTitle>
            <DialogDescription>
              {t('profiles.deleteConfirm', { name: deleting?.name })}
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setDeleting(null)}>
              {t('profiles.cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (deleting) deleteProfile(deleting.id)
                setDeleting(null)
              }}
            >
              {t('profiles.delete')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
