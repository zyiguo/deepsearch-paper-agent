import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'
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
