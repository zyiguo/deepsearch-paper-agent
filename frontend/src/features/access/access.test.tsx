import { afterEach, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { api } from '../../shared/api'
import { apiUrl, configureAccessScope, sessionSelectionKey } from '../../shared/accessScope'
import { accessApi } from './accessApi'
import { LoginPanel } from './AccessGate'
import { sourceImageUrl } from '../evidence/pdfSource'
import ImageAttachments from '../chat/ImageAttachments'

const workspace = 'a'.repeat(32)
afterEach(() => { configureAccessScope(null); vi.unstubAllGlobals() })

it('binds JSON writes to an explicit workspace and sends the CSRF request header', async () => {
  configureAccessScope(workspace, 'alice')
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id:'session', title:'test', created_at:'now', updated_at:'now' })))
  vi.stubGlobal('fetch', fetch)
  await api.createSession('test')
  expect(fetch.mock.calls[0][0]).toBe(`/w/${workspace}/api/sessions`)
  expect(new Headers(fetch.mock.calls[0][1].headers).get('X-DeepSearch-Request')).toBe('1')
})

it('binds SSE and evidence image URLs to the same workspace', async () => {
  configureAccessScope(workspace, 'alice')
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail:'fixture denial' }), {status:403}))
  vi.stubGlobal('fetch', fetch)
  await expect(api.streamTurn('session', 'question', 3, () => {})).rejects.toThrow('fixture denial')
  expect(fetch.mock.calls[0][0]).toBe(`/w/${workspace}/api/sessions/session/turns/stream`)
  expect(new Headers(fetch.mock.calls[0][1].headers).get('X-DeepSearch-Request')).toBe('1')
  expect(sourceImageUrl(`pdf://${'b'.repeat(64)}/fixture.pdf`, 1)).toBe(`/w/${workspace}/api/documents/${'b'.repeat(64)}/pages/1/image`)
  const html = renderToStaticMarkup(<ImageAttachments attachments={[{id:'image', title:'fixture', source_path:'fixture', image_url:'/api/sessions/session/attachments/image/image', width:10, height:10}]} />)
  expect(html).toContain(`/w/${workspace}/api/sessions/session/attachments/image/image`)
})

it('keeps session selections separate by user and workspace and preserves the local key', () => {
  expect(sessionSelectionKey()).toBe('deepsearch.active-session')
  configureAccessScope(workspace, 'alice')
  const first = sessionSelectionKey()
  configureAccessScope(workspace, 'bob')
  expect(sessionSelectionKey()).not.toBe(first)
  configureAccessScope('b'.repeat(32), 'alice')
  expect(sessionSelectionKey()).not.toBe(first)
  expect(() => configureAccessScope('../outside')).toThrow()
  expect(() => apiUrl('https://external.example')).toThrow()
})

it('keeps identity requests outside the workspace and does not store credentials in browser storage', async () => {
  configureAccessScope(workspace, 'alice')
  const storage = { setItem: vi.fn() }
  vi.stubGlobal('localStorage', storage)
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ authenticated: true })))
  vi.stubGlobal('fetch', fetch)
  await accessApi.login('alice', 'synthetic-password')
  expect(fetch.mock.calls[0][0]).toBe('/api/access/login')
  expect(fetch.mock.calls[0][1].credentials).toBe('same-origin')
  expect(fetch.mock.calls[0][1].cache).toBe('no-store')
  expect(storage.setItem).not.toHaveBeenCalled()
})

it('renders an accessible password login with explicit error and busy states', () => {
  const html = renderToStaticMarkup(<LoginPanel onSubmit={() => {}} busy error="登录失败" />)
  expect(html).toContain('type="password"')
  expect(html).toContain('autoComplete="current-password"')
  expect(html).toContain('role="alert"')
  expect(html).toContain('正在登录')
  expect(html).toContain('disabled=""')
})
