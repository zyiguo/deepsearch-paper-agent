import type { Source } from '../../shared/api'
import { libraryDocumentId, sourceImageUrl, sourceTitle } from './pdfSource'

export function sourcePageLabel(source: Source) {
  return source.page_end && source.page_end !== source.page_number
    ? `第 ${source.page_number}–${source.page_end} 页` : `第 ${source.page_number} 页`
}

export default function EvidenceSource({ source, onOpenSource }: {
  source: Source; onOpenSource: (id: string, page: number) => void
}) {
  const id = libraryDocumentId(source.source_path)
  const pages = source.pages?.length ? source.pages : [{ page_number: source.page_number, text: source.text }]
  const kind = { text: '原文', ocr: '扫描文字识别', figure: '图', table: '表', formula: '公式', image: '原页图像' }[source.evidence_kind ?? 'text']
  return <article className="evidence-card" data-chunk-id={source.chunk_id} data-content-id={source.content_id}>
    <span className="citation">[{source.citation_id}]</span>
    <div className="evidence-body">
      <div className="source-header"><h3>{sourceTitle(source.source_path)}</h3><span className="page-tag">{sourcePageLabel(source)}</span></div>
      {!!source.section_path?.length && <p className="source-section">{source.section_path.join(' › ')}</p>}
      {source.evidence_kind && source.evidence_kind !== 'text' && <p className="source-section">{kind}</p>}
      {!!source.visual_pages?.length && <div className="visual-evidence-pages">
        {source.visual_pages.map(page => {
          const url = sourceImageUrl(source.source_path, page)
          if (!url) return null
          return id ? <button key={page} className="visual-evidence-page" title={`查看第 ${page} 页原图`} onClick={() => onOpenSource(id, page)}>
            <img loading="lazy" src={url} alt={`第 ${page} 页原图`} /><span>第 {page} 页</span>
          </button> : <a key={page} className="visual-evidence-page" href={url} target="_blank" rel="noreferrer" title="查看提问附件">
            <img loading="lazy" src={url} alt={sourceTitle(source.source_path)} /><span>提问附件</span>
          </a>
        })}
      </div>}
      {pages.map(page => <div className="source-page" key={page.page_number}>
        {id ? <button className="page-tag" onClick={() => onOpenSource(id, page.page_number)}>第 {page.page_number} 页 · 查看原文</button>
          : pages.length > 1 && <span className="page-tag">第 {page.page_number} 页</span>}
        {source.evidence_kind !== 'image' && <p className="source-text">{page.text}</p>}
      </div>)}
      <p className="source-path">{source.source_path}</p>
    </div>
  </article>
}
