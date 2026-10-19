export function pdfDocumentId(source: string): string | null {
  return /^pdf:\/\/([a-f0-9]{64})\//.exec(source)?.[1] ?? null
}
export function libraryDocumentId(source: string): string | null {
  return /^(?:pdf|image):\/\/([a-f0-9]{64})\//.exec(source)?.[1] ?? null
}
export function sourceImageUrl(source: string, page: number): string | null {
  const id = libraryDocumentId(source)
  if (id) return apiUrl(`/api/documents/${id}/pages/${page}/image`)
  const attachment = /^attachment:\/\/([a-f0-9-]{36})\/([a-f0-9]{64})\//.exec(source)
  return attachment ? apiUrl(`/api/sessions/${attachment[1]}/attachments/${attachment[2]}/image`) : null
}
export function sourceTitle(source: string): string {
  const name = source.replaceAll('\\', '/').split('/').pop() || source
  try { return decodeURIComponent(name) } catch { return name }
}
import { apiUrl } from '../../shared/accessScope'

