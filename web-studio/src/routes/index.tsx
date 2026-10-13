import { useStudioService } from '#/hooks/use-studio-service'
import { Navigate, createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/')({
  component: IndexRoute,
})

function IndexRoute() {
  const { provider } = useStudioService()
  return (
    <Navigate
      replace
      to={provider === 'opensource' ? '/home' : '/filesystem'}
    />
  )
}
