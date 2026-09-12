import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'

const memory = { version: 1, revision: 2, document_ids: ['a'], research_goal: '比较方法', language: 'zh',
  answer_style: '简洁', summary: '讨论主题', active_entities: ['AX-247'], open_questions: ['成本？'],
  compression_status: 'ready', summary_through_turn: 4 }
afterEach(() => vi.unstubAllGlobals())
function mock() {
  const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify(memory)))
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}
it('loads structured memory without losing the summary or scope', async () => {
  mock()
  expect(await api.memory('session')).toEqual(memory)
})
it('clears memory separately from deleting the conversation', async () => {
  const fetcher = mock()
  await api.clearMemory('session')
  expect(fetcher.mock.calls[0][0]).toBe('/api/sessions/session/memory')
  expect(fetcher.mock.calls[0][1].method).toBe('DELETE')
})
it('persists an empty document selection without broadening it', async () => {
  const fetcher = mock()
  await api.setMemoryScope('session', [])
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ document_ids: [] })
})
it('edits only user-owned memory settings', async () => {
  const fetcher = mock()
  const values = { research_goal: '方法比较', language: 'zh', answer_style: '简洁' }
  await api.editMemory('session', values)
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual(values)
})
