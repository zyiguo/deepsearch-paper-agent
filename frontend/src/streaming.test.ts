import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'

afterEach(() => vi.unstubAllGlobals())
const turn = { id: 1, question: '问题', created_at: '2026-09-11', answer: { text: '两块 GPU', sources: [] } }
function reply(text: string) {
  const bytes = new TextEncoder().encode(text)
  const fetcher = vi.fn().mockResolvedValue(new Response(new ReadableStream({
    start(controller) {
      // Break every UTF-8 sequence and every SSE boundary across reads.
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]))
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } }))
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}
it('receives progress and verified text across arbitrary UTF-8 chunks', async () => {
  reply('event: status\ndata: {"stage":"verifying"}\n\n: heartbeat\n\nevent: answer\ndata: {"text":"两块 GPU","sources":[]}\n\nevent: done\ndata: ' + JSON.stringify(turn) + '\n\n')
  const events: unknown[] = []
  expect(await api.streamTurn('one', '问题', 3, event => events.push(event))).toEqual(turn)
  expect(events).toEqual([{ type: 'status', stage: 'verifying' }, { type: 'answer', text: '两块 GPU', sources: [] }])
})
it('rejects an interrupted stream without resending', async () => {
  const fetcher = reply('event: status\ndata: {"stage":"verifying"}\n\n')
  await expect(api.streamTurn('one', '问题', 3, () => {})).rejects.toThrow('中断')
  expect(fetcher).toHaveBeenCalledTimes(1)
})
it('surfaces a terminal error rather than claiming completion', async () => {
  reply('event: error\ndata: {"message":"回答未完成"}\n\n')
  await expect(api.streamTurn('one', '问题', 3, () => {})).rejects.toThrow('回答未完成')
})
it('sends the exact selected document IDs, including an empty selection', async () => {
  const fetcher = reply('event: done\ndata: ' + JSON.stringify(turn) + '\n\n')
  await api.streamTurn('one', '问题', 3, () => {}, undefined, ['chosen'])
  expect(JSON.parse(fetcher.mock.calls[0][1].body).document_ids).toEqual(['chosen'])
  await api.streamTurn('one', '问题', 3, () => {}, undefined, []).catch(() => {})
  expect(JSON.parse(fetcher.mock.calls[1][1].body).document_ids).toEqual([])
})
