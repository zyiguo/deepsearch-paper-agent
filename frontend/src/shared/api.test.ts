import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'

afterEach(() => vi.unstubAllGlobals())

describe('HTTP contract', () => {
  it('preserves persistent evidence identities in answer references', async () => {
    const source = {
      citation_id: 1, source_path: 'paper.pdf', page_number: 1, text: 'Evidence',
      chunk_id: 'chk_' + 'a'.repeat(64), content_id: 'cnt_' + 'b'.repeat(64),
      asset_ids: ['ast_' + 'c'.repeat(64)],
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      mode: 'live-llm-live-retrieval', text: 'Answer [1]', sources: [source],
    }))))
    expect((await api.ask('question', 1)).sources).toEqual([source])
  })
  it('sends question and top_k and accepts an evidence-free refusal', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      mode: 'demo', text: '未检索到证据', sources: [],
    })))
    vi.stubGlobal('fetch', fetchMock)
    expect((await api.ask('模型怎么训练？', 1)).sources).toEqual([])
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      question: '模型怎么训练？', top_k: 1,
    })
  })
  it('preserves the backend demo restriction message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: '演示模式仅支持两个问题' }), { status: 400 },
    )))
    await expect(api.ask('任意问题', 1)).rejects.toThrow('演示模式仅支持两个问题')
  })
  it('formats FastAPI validation errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: [{ msg: '问题不能为空' }] }), { status: 422 },
    )))
    await expect(api.ask('', 1)).rejects.toThrow('问题不能为空')
  })
  it('rejects malformed successful responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }))))
    await expect(api.documents()).rejects.toThrow('数据格式')
  })
  it('explains proxy failures that are not JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('proxy error', { status: 502 })))
    await expect(api.documents()).rejects.toThrow('HTTP 502')
  })
  it('explains an unavailable backend', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(api.health()).rejects.toThrow('8000')
  })
})

describe('persistence HTTP contract', () => {
  it('reads dirty and write-blocked states without discarding the backend error', async () => {
    const state = { snapshot_exists: true, chunk_count: 2, dirty: true, write_blocked: true, error: '模型标识不一致' }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(state))))
    expect(await api.persistenceStatus()).toEqual(state)
  })
  it('rejects missing persistence fields', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ snapshot_exists: true }))))
    await expect(api.persistenceStatus()).rejects.toThrow('数据格式')
  })
  it.each([
    ['save', '/api/knowledge-base/save'],
    ['load', '/api/knowledge-base/load'],
  ] as const)('uses POST for %s without accepting a browser file path', async (action, path) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ message: '操作成功' })))
    vi.stubGlobal('fetch', fetchMock)
    await (action === 'save' ? api.saveKnowledgeBase() : api.loadKnowledgeBase())
    expect(fetchMock.mock.calls[0][0]).toBe(path)
    expect(fetchMock.mock.calls[0][1].method).toBe('POST')
    expect(fetchMock.mock.calls[0][1].body).toBeUndefined()
  })
  it('surfaces a rejected overwrite as an error, not success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '禁止覆盖' }), { status: 409 })))
    await expect(api.saveKnowledgeBase()).rejects.toThrow('禁止覆盖')
  })
})
