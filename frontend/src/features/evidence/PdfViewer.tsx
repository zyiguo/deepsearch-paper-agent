import { useEffect, useRef, useState } from 'react'
import { api } from '../../shared/api'
import { apiUrl } from '../../shared/accessScope'
import './pdf.css'

export default function PdfViewer({ id, initialPage, onClose }: { id: string; initialPage: number; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [page, setPage] = useState(initialPage)
  const [data, setData] = useState<Awaited<ReturnType<typeof api.documentPage>> | null>(null)
  const [error, setError] = useState('')
  const [imageError, setImageError] = useState(false)
  const [imageLoaded, setImageLoaded] = useState(false)
  const [textOnly, setTextOnly] = useState(false)
  useEffect(() => {
    const previous = document.activeElement
    const target = dialog.current
    target?.showModal()
    return () => { target?.close(); if (previous instanceof HTMLElement) previous.focus() }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    setData(null); setError(''); setImageError(false); setImageLoaded(false)
    api.documentPage(id, page, AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]))
      .then(value => { if (!controller.signal.aborted) setData(value) })
      .catch(e => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '页面加载失败') })
    return () => controller.abort()
  }, [id, page])
  const isImage = data?.media_type === 'image/png'
  return <dialog ref={dialog} className="pdf-dialog" aria-label="原文查看" onCancel={e => { e.preventDefault(); onClose() }}>
    <header className="pdf-toolbar"><div><small>ORIGINAL DOCUMENT</small><h2>{data?.title ?? '原文查看'}</h2></div><button onClick={onClose} autoFocus>关闭 ×</button></header>
    <nav className="pdf-controls" aria-label="PDF 翻页">
      <button disabled={page <= 1} onClick={() => setPage(p => p - 1)}>上一页</button>
      <span>第 {page} / {data?.page_count ?? '…'} 页</span>
      <button disabled={!data || page >= data.page_count} onClick={() => setPage(p => p + 1)}>下一页</button>
      <button onClick={() => setTextOnly(value => !value)}>{textOnly ? '查看原页' : '查看提取文字'}</button>
      <a href={apiUrl(`/api/documents/${encodeURIComponent(id)}/file`)}>{isImage ? '下载图片' : '下载原始 PDF'}</a>
    </nav>
    <div className="pdf-canvas">
      {error && <p role="alert" className="error">{error}</p>}
      {!data && !error && <p role="status">正在加载原文…</p>}
      {data && (textOnly || imageError) ? <><p className="pdf-caption">{imageError ? '原页渲染失败，以下为提取文字。' : '提取文字可能与原页排版不同。'}</p><pre>{data.text || '本页没有可提取的文字，可能需要 OCR。'}</pre></> : data && <>
        {!imageLoaded && <p role="status">正在渲染第 {page} 页…</p>}
        <img key={`${id}-${page}`} src={apiUrl(`/api/documents/${encodeURIComponent(id)}/pages/${page}/image`)} alt={`${data.title} 第 ${page} 页原文`} onLoad={() => setImageLoaded(true)} onError={() => setImageError(true)} />
      </>}
    </div>
  </dialog>
}
