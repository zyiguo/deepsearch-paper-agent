/** One scope per mounted application. Workspace switches perform full navigation. */
let workspace: string | null = null
let user = 'local'

export function configureAccessScope(id: string | null, userId = 'local') {
  if (id !== null && !/^[a-f0-9]{32}$/.test(id)) throw new Error('无效工作空间')
  workspace = id
  user = userId
}

export function apiUrl(path: string): string {
  if (!path.startsWith('/api/')) throw new Error('仅允许工作台 API 路径')
  return workspace ? `/w/${workspace}${path}` : path
}

export function sessionSelectionKey() {
  return workspace ? `deepsearch.active-session.${user}.${workspace}` : 'deepsearch.active-session'
}

export function mutationHeaders(headers?: HeadersInit) {
  const result = new Headers(headers)
  result.set('X-DeepSearch-Request', '1')
  return Object.fromEntries(result.entries())
}

export function notifyUnauthorized(status: number) {
  if (status === 401 && typeof window !== 'undefined') window.dispatchEvent(new Event('deepsearch:unauthorized'))
}
