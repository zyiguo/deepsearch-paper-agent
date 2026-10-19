import { afterEach, expect, it, vi } from 'vitest'
import { api } from '../../shared/api'

afterEach(() => vi.unstubAllGlobals())
const doc = { id: 'a'.repeat(64), title: '小说.pdf', source_path: `pdf://${'a'.repeat(64)}/story.pdf`,
  chunk_count: 0, page_count: 0, status: 'queued', error: null, warnings: [], file_url: null }

it('uploads PDF bytes with an encoded display filename', async () => {
  const mock = vi.fn().mockResolvedValue(new Response(JSON.stringify(doc), { status: 202 }))
  vi.stubGlobal('fetch', mock)
  const file = new File(['%PDF-test'], '小说.pdf', { type: 'application/pdf' })
  expect((await api.uploadPdf(file)).status).toBe('queued')
  expect(mock.mock.calls[0][0]).toBe('/api/documents/upload?filename=%E5%B0%8F%E8%AF%B4.pdf')
  expect(mock.mock.calls[0][1].body).toBe(file)
})
it('loads extracted page text and count', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: doc.id, title: '小说.pdf', page_number: 2, page_count: 8, text: '原文' }))))
  expect((await api.documentPage(doc.id, 2)).text).toBe('原文')
})
it('retains failure details and partial-page warnings in library list', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify([{ ...doc, status: 'failed', error: '需要 OCR', warnings: ['空白页'] }]))))
  const result = await api.documents()
  expect(result[0].error).toBe('需要 OCR')
  expect(result[0].warnings).toEqual(['空白页'])
})
