import { afterEach, expect, it, vi } from 'vitest'
import { api } from '../../shared/api'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import EvidenceSource from './EvidenceSource'

afterEach(() => vi.unstubAllGlobals())
it('preserves section and cross-page evidence returned by the backend', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    mode: 'live-llm-live-retrieval', text: 'Uses two GPUs [1]', sources: [{
      citation_id: 1, source_path: 'paper.pdf', text: 'uses\ntwo GPUs', page_number: 4, page_end: 5,
      section_path: ['2 Methods'], pages: [{ page_number: 4, text: 'uses' }, { page_number: 5, text: 'two GPUs' }],
    }],
  }))))
  const result = await api.ask('Hardware?', 3)
  expect(result.sources[0]).toMatchObject({ page_end: 5, section_path: ['2 Methods'], pages: [{ page_number: 4, text: 'uses' }, { page_number: 5, text: 'two GPUs' }] })
})

it('renders separate original-page buttons for cross-page evidence', () => {
  const html = renderToStaticMarkup(createElement(EvidenceSource, {
    source: { citation_id: 1, source_path: `pdf://${'a'.repeat(64)}/paper.pdf`, page_number: 4, page_end: 5,
      text: 'uses\ntwo GPUs', section_path: ['2 Methods', '2.1 Training'],
      pages: [{ page_number: 4, text: 'uses' }, { page_number: 5, text: 'two GPUs' }] },
    onOpenSource: () => {},
  }))
  expect(html).toContain('第 4–5 页')
  expect(html).toContain('第 4 页 · 查看原文')
  expect(html).toContain('第 5 页 · 查看原文')
  expect(html).toContain('2 Methods › 2.1 Training')
})

it('continues to render old single-page history without structural metadata', () => {
  const html = renderToStaticMarkup(createElement(EvidenceSource, {
    source: { citation_id: 1, source_path: 'paper.pdf', page_number: 2, text: 'Legacy evidence' },
    onOpenSource: () => {},
  }))
  expect(html).toContain('第 2 页')
  expect(html).toContain('Legacy evidence')
})

it('preserves visual citation metadata and renders the corresponding original page', async () => {
  const source = { citation_id: 1, source_path: `pdf://${'b'.repeat(64)}/scan.pdf`, page_number: 3,
    text: 'Table 1: 42 V', evidence_kind: 'table', visual_pages: [3] }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    mode: 'live-llm-live-retrieval', text: '42 V [1]', sources: [source],
  }))))
  const result = await api.ask('Voltage?', 3)
  expect(result.sources[0]).toMatchObject({ evidence_kind: 'table', visual_pages: [3] })
  const html = renderToStaticMarkup(createElement(EvidenceSource, { source: result.sources[0], onOpenSource: () => {} }))
  expect(html).toContain(`/api/documents/${'b'.repeat(64)}/pages/3/image`)
  expect(html).toContain('查看第 3 页原图')
})

it('accepts direct image hits and displays their original page', async () => {
  const source = { citation_id: 1, source_path: `pdf://${'c'.repeat(64)}/chart.pdf`, page_number: 2,
    text: 'Original page image; no OCR', evidence_kind: 'image', visual_pages: [2] }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    mode: 'live-llm-live-retrieval', text: 'Rising curve [1]', sources: [source],
  }))))
  const result = await api.ask('Curve?', 3)
  expect(result.sources[0].evidence_kind).toBe('image')
  const html = renderToStaticMarkup(createElement(EvidenceSource, { source: result.sources[0], onOpenSource: () => {} }))
  expect(html).toContain('原页图像')
  expect(html).toContain(`/api/documents/${'c'.repeat(64)}/pages/2/image`)
  expect(html).not.toContain('Original page image; no OCR')
})

it('preserves image indexing state and calls the separate backfill endpoint', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    id: 'd'.repeat(64), title: 'scan.pdf', source_path: 'scan.pdf', chunk_count: 0,
    page_count: 2, image_index_available: true, image_indexed_pages: 2,
  })))
  vi.stubGlobal('fetch', fetch)
  const doc = await api.indexDocumentImages('d'.repeat(64))
  expect(doc.image_indexed_pages).toBe(2)
  expect(fetch.mock.calls[0][0]).toBe(`/api/documents/${'d'.repeat(64)}/index-images`)
  expect(fetch.mock.calls[0][1].method).toBe('POST')
})
