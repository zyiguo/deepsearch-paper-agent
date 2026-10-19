import { afterEach, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { api } from '../../shared/api'
import ImageAttachments from './ImageAttachments'
import EvidenceSource from '../evidence/EvidenceSource'
import { libraryDocumentId, sourceImageUrl } from '../evidence/pdfSource'
import { validateImageFile } from '../../shared/imageFiles'

const session = '12345678-1234-1234-1234-123456789abc'
const id = 'a'.repeat(64)
const image = { id, title: 'chart.png', source_path: `attachment://${session}/${id}/chart.png`,
  image_url: `/api/sessions/${session}/attachments/${id}/image`, width: 80, height: 60 }
afterEach(() => vi.unstubAllGlobals())

it('uploads image bytes into the selected session with the correct MIME type', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(image)))
  vi.stubGlobal('fetch', fetch)
  const file = new File(['image bytes'], 'chart.png')
  expect(await api.uploadAttachment(session, file)).toEqual(image)
  expect(fetch.mock.calls[0][0]).toContain(`/api/sessions/${session}/attachments?filename=chart.png`)
  expect(new Headers(fetch.mock.calls[0][1].headers).get('Content-Type')).toBe('image/png')
  expect(fetch.mock.calls[0][1].body).toBe(file)
})

it('sends attachments with an empty library scope and preserves the terminal turn', async () => {
  const turn = { id: 1, question: 'Plot?', created_at: 'now', answer: { text: 'Blue [1]', sources: [] }, attachments: [image] }
  const fetch = vi.fn().mockResolvedValue(new Response(`event: done\ndata: ${JSON.stringify(turn)}\n\n`,
    { headers: { 'Content-Type': 'text/event-stream' } }))
  vi.stubGlobal('fetch', fetch)
  expect(await api.streamTurn(session, 'Plot?', 3, () => {}, undefined, [], [id])).toEqual(turn)
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toMatchObject({ document_ids: [], attachment_ids: [id] })
})

it('shows preview, removal and promotion controls without changing the image identity', () => {
  const html = renderToStaticMarkup(createElement(ImageAttachments, {
    attachments: [image], onRemove: () => {}, onIndex: () => {},
  }))
  expect(html).toContain(image.image_url)
  expect(html).toContain('移除附件 chart.png')
  expect(html).toContain('加入资料库')
})

it('resolves library images and session image citations to local previews', () => {
  expect(libraryDocumentId(`image://${id}/chart.png`)).toBe(id)
  expect(sourceImageUrl(image.source_path, 1)).toBe(image.image_url)
  expect(sourceImageUrl('https://remote.test/image.png', 1)).toBeNull()
  const html = renderToStaticMarkup(createElement(EvidenceSource, {
    source: { citation_id: 1, source_path: image.source_path, text: 'attachment', page_number: 1,
      evidence_kind: 'image', visual_pages: [1] }, onOpenSource: () => {},
  }))
  expect(html).toContain(image.image_url)
  expect(html).toContain('提问附件')
})

it('rejects unsupported, empty and oversized attachment selections', () => {
  expect(validateImageFile(new File(['x'], 'chart.svg'))).not.toBeNull()
  expect(validateImageFile(new File([], 'chart.png'))).not.toBeNull()
  expect(validateImageFile(new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'chart.jpg'))).not.toBeNull()
  expect(validateImageFile(new File(['x'], 'chart.webp'))).toBeNull()
})
