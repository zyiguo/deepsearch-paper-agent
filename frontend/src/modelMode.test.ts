import { afterEach, expect, it, vi } from 'vitest'
import { modelMode } from './modelMode'
import { api } from './api'

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('distinguishes live generation from demo retrieval', () => {
  expect(modelMode('live-llm-demo-retrieval').label).toBe('真实模型 · 演示检索')
  expect(modelMode('live-llm-demo-retrieval').detail).toContain('仍仅支持两个示例问题')
  expect(modelMode('demo').description).toContain('固定回答')
})
it('does not assume unknown modes are live', () => {
  expect(modelMode(null).tag).toBe('INFO')
  expect(modelMode('future-mode').tag).toBe('INFO')
})
it('gives generation requests a longer deadline but keeps health checks short', async () => {
  const timeout = vi.spyOn(AbortSignal, 'timeout')
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (path: string) => new Response(JSON.stringify(
    path === '/api/health' ? { status: 'ok', mode: 'live-llm-demo-retrieval' }
      : { id: 1, question: '问题', created_at: 'now', answer: { text: '回答', sources: [] } },
  ))))
  await api.sendTurn('session', '问题', 1)
  expect(timeout).toHaveBeenLastCalledWith(330000)
  await api.health()
  expect(timeout).toHaveBeenLastCalledWith(15000)
})
it('explains uncertain completion on timeout and does not retry', async () => {
  const mock = vi.fn().mockRejectedValue(new DOMException('timeout', 'TimeoutError'))
  vi.stubGlobal('fetch', mock)
  await expect(api.sendTurn('session', '问题', 1)).rejects.toThrow('不要直接重复发送')
  expect(mock).toHaveBeenCalledTimes(1)
})
it('preserves sanitized model-service errors', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '模型认证失败，请检查密钥与接口地址是否匹配。' }), { status: 502 })))
  await expect(api.sendTurn('session', '问题', 1)).rejects.toThrow('模型认证失败')
})
