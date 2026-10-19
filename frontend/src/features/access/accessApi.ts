import { z } from 'zod'
import { ApiError } from '../../shared/api'
import { mutationHeaders } from '../../shared/accessScope'

const userSchema = z.object({ id: z.string(), username: z.string(), display_name: z.string(), is_admin: z.boolean(), disabled: z.boolean() })
const workspaceSchema = z.object({ id: z.string().regex(/^[a-f0-9]{32}$/), name: z.string(), role: z.enum(['owner', 'editor', 'viewer']) })
const memberSchema = userSchema.omit({ is_admin: true }).extend({ role: z.enum(['owner', 'editor', 'viewer']) })
export type Workspace = z.infer<typeof workspaceSchema>
export type AccessUser = z.infer<typeof userSchema>
export type Member = z.infer<typeof memberSchema>
export type Identity = { user: AccessUser; workspaces: Workspace[] }

async function control<T>(path: string, schema: z.ZodType<T>, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method, credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000),
    headers: mutationHeaders(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const value: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = z.object({ detail: z.string() }).safeParse(value)
    throw new ApiError(detail.success ? detail.data.detail : '账号服务暂时不可用', response.status)
  }
  return schema.parse(value)
}

export const accessApi = {
  status: () => control('/api/access/status', z.object({ mode: z.enum(['local', 'team']) })),
  me: (): Promise<Identity> => control('/api/access/me', z.object({ user: userSchema, workspaces: z.array(workspaceSchema) })),
  login: (username: string, password: string) => control('/api/access/login', z.object({ authenticated: z.boolean() }), 'POST', { username, password }),
  logout: () => control('/api/access/logout', z.object({ authenticated: z.boolean() }), 'POST'),
  changePassword: (current_password: string, new_password: string) => control('/api/access/password', z.object({ changed: z.boolean() }), 'POST', { current_password, new_password }),
  createWorkspace: (name: string) => control('/api/workspaces', workspaceSchema, 'POST', { name }),
  members: (id: string) => control(`/api/workspaces/${id}/members`, z.array(memberSchema)),
  setMember: (id: string, username: string, role: 'editor' | 'viewer') => control(`/api/workspaces/${id}/members`, z.object({ updated: z.boolean() }), 'PUT', { username, role }),
  removeMember: (id: string, user: string) => control(`/api/workspaces/${id}/members/${user}`, z.object({ removed: z.boolean() }), 'DELETE'),
  users: () => control('/api/admin/users', z.array(userSchema)),
  createUser: (username: string, display_name: string, password: string) => control('/api/admin/users', userSchema, 'POST', { username, display_name, password }),
  disableUser: (id: string) => control(`/api/admin/users/${id}/disable`, z.object({ disabled: z.boolean() }), 'POST'),
  enableUser: (id: string) => control(`/api/admin/users/${id}/enable`, z.object({ enabled: z.boolean() }), 'POST'),
}
