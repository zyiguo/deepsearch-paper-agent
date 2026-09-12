export function pdfDocumentId(source: string): string | null {
  return /^pdf:\/\/([a-f0-9]{64})\//.exec(source)?.[1] ?? null
}
export function sourceTitle(source: string): string {
  const name = source.replaceAll('\\', '/').split('/').pop() || source
  try { return decodeURIComponent(name) } catch { return name }
}
