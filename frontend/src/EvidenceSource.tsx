import type { Source } from './api'
import { pdfDocumentId, sourceTitle } from './pdfSource'

export function sourcePageLabel(source: Source) {
  return source.page_end && source.page_end !== source.page_number
    ? `第 ${source.page_number}–${source.page_end} 页` : `第 ${source.page_number} 页`
}

export default function EvidenceSource({ source, onOpenSource }: {
  source: Source; onOpenSource: (id: string, page: number) => void
}) {
  const id = pdfDocumentId(source.source_path)
  const pages = source.pages?.length ? source.pages : [{ page_number: source.page_number, text: source.text }]
  return <article className="evidence-card">
    <span className="citation">[{source.citation_id}]</span>
    <div className="evidence-body">
      <div className="source-header"><h3>{sourceTitle(source.source_path)}</h3><span className="page-tag">{sourcePageLabel(source)}</span></div>
      {!!source.section_path?.length && <p className="source-section">{source.section_path.join(' › ')}</p>}
      {pages.map(page => <div className="source-page" key={page.page_number}>
        {id ? <button className="page-tag" onClick={() => onOpenSource(id, page.page_number)}>第 {page.page_number} 页 · 查看原文</button>
          : pages.length > 1 && <span className="page-tag">第 {page.page_number} 页</span>}
        <p className="source-text">{page.text}</p>
      </div>)}
      <p className="source-path">{source.source_path}</p>
    </div>
  </article>
}
