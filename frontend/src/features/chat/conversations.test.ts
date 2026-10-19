import { afterEach, expect, it, vi } from 'vitest'
import { api } from '../../shared/api'

afterEach(() => vi.unstubAllGlobals())
const session = { id: 'first', title: '训练研究', created_at: '2026-09-09T00:00:00Z', updated_at: '2026-09-09T00:00:00Z' }
const turn = { id: 1, question: '模型怎么训练？', created_at: session.created_at, answer: { text: '回答 [1]', sources: [{ citation_id: 1, source_path: 'demo.pdf', page_number: 2, text: '对比学习' }] } }
it('deletes only the selected session through DELETE', async () => {
  const mock = reply({ message: '已删除' })
  expect(await api.deleteSession('selected')).toEqual({ message: '已删除' })
  expect(mock.mock.calls[0][0]).toBe('/api/sessions/selected')
  expect(mock.mock.calls[0][1].method).toBe('DELETE')
})
it('surfaces failed deletion without automatic retry', async () => {
  const mock = reply({ detail: '无法删除' }, 500)
  await expect(api.deleteSession('selected')).rejects.toThrow('无法删除')
  expect(mock).toHaveBeenCalledTimes(1)
})
function reply(body: unknown, status = 200) {
  const mock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status }))
  vi.stubGlobal('fetch', mock)
  return mock
}
it('lists persisted sessions', async () => {
  reply([session])
  expect(await api.sessions()).toEqual([session])
})
it('creates a named session with POST', async () => {
  const mock = reply(session, 201)
  await api.createSession('训练研究')
  expect(mock.mock.calls[0][0]).toBe('/api/sessions')
  expect(mock.mock.calls[0][1].method).toBe('POST')
  expect(JSON.parse(mock.mock.calls[0][1].body)).toEqual({ title: '训练研究' })
})
it('restores each turn and its evidence without requiring legacy mode', async () => {
  reply({ ...session, turns: [turn, { ...turn, id: 2, answer: { text: '无证据', sources: [] } }] })
  const result = await api.session('first')
  expect(result.turns[0].answer.sources[0].page_number).toBe(2)
  expect(result.turns[1].answer.sources).toEqual([])
})
it('sends to the selected session with the current retrieval count', async () => {
  const mock = reply(turn, 201)
  expect(await api.sendTurn('a/b', '模型怎么训练？', 1)).toEqual(turn)
  expect(mock.mock.calls[0][0]).toBe('/api/sessions/a%2Fb/turns')
  expect(JSON.parse(mock.mock.calls[0][1].body)).toEqual({ question: '模型怎么训练？', top_k: 1 })
})
it('rejects malformed history instead of displaying corrupt evidence', async () => {
  reply({ ...session, turns: [{ ...turn, answer: { text: 'x', sources: [{}] } }] })
  await expect(api.session('first')).rejects.toThrow('数据格式')
})
it('does not retry a failed send automatically', async () => {
  const mock = reply({ detail: '服务暂时不可用' }, 500)
  await expect(api.sendTurn('first', '模型怎么训练？', 1)).rejects.toThrow('服务暂时不可用')
  expect(mock).toHaveBeenCalledTimes(1)
})
