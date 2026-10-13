import type { ServiceProvider } from './studio-service'
import type { ConnectionRole } from '#/hooks/use-app-connection'
import type { ServerMode } from '#/hooks/use-server-mode'

export type StudioManagementCapabilities = {
  canManageAccounts: boolean
  canManageUsers: boolean
}

export function resolveStudioManagementCapabilities({
  serviceProvider = 'opensource',
  hasControlCredential,
  isRoleLoading,
  role,
  serverMode,
}: {
  serviceProvider?: ServiceProvider
  hasControlCredential: boolean
  isRoleLoading: boolean
  role: ConnectionRole
  serverMode: ServerMode
}): StudioManagementCapabilities {
  if (
    serviceProvider !== 'opensource' ||
    isRoleLoading ||
    serverMode === 'dev' ||
    !hasControlCredential
  ) {
    return {
      canManageAccounts: false,
      canManageUsers: false,
    }
  }

  return {
    canManageAccounts: role === 'root',
    canManageUsers: role === 'root' || role === 'admin',
  }
}

type ManagementInput = Parameters<typeof resolveStudioManagementCapabilities>[0]

export function canManageStudioGateway(input: ManagementInput): boolean {
  return (
    input.serverMode === 'api_key' &&
    resolveStudioManagementCapabilities(input).canManageUsers
  )
}

export function canEditStudioConfiguration(input: ManagementInput): boolean {
  return (
    (input.serverMode === 'api_key' || input.serverMode === 'trusted') &&
    resolveStudioManagementCapabilities(input).canManageAccounts
  )
}
