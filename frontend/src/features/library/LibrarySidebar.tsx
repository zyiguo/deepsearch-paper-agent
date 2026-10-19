import { libraryDocumentId } from '../evidence/pdfSource'
import type { Dispatch, SetStateAction } from 'react'
import type { PaperDocument } from '../../shared/api'
import Icon from '../../shared/Icon'
import { imageAccept } from '../../shared/imageFiles'
import type { LibraryController } from './useLibrary'

type Props = {
  readOnly?: boolean
  library: LibraryController
  asking: boolean
  chosenIds: string[]
  selectable: PaperDocument[]
  setSelectedIds: Dispatch<SetStateAction<string[] | null>>
  setViewer: (value: { id: string; page: number } | null) => void
}

export default function LibrarySidebar({ library, readOnly = false, asking, chosenIds, selectable, setSelectedIds, setViewer }: Props) {
  const { documents, busy, file, setFile, consent, setConsent, filter, setFilter, fileInput, persistence, persistenceIssue, confirmReload, setConfirmReload, connection, syncLibrary, uploadPdf, retryDocument, persist, requestReload, working, storage, canUpload } = library
  const visibleDocuments = documents.filter(document => document.title.toLowerCase().includes(filter.toLowerCase()))
  const statusLabel: Record<string, string> = { queued: '排队中', parsing: '解析 PDF 中', analyzing: '识别图表与扫描页中', embedding: '生成向量中', ready: '可提问', failed: '处理失败', not_indexed: '需重新入库' }
  return (
      <aside className="sidebar" aria-label="文献库">
        <a className="brand" href="/" aria-label="DeepSearch 首页"><span className="brand-symbol">D<span>·</span></span><span><strong>DeepSearch</strong></span></a>
        <div className="library-heading"><h2>文献库</h2><span className="count">{documents.length}</span></div>
        <section className="pdf-upload" aria-label="上传资料">
          <label htmlFor="pdf-file">上传 PDF 或图片</label>
          <input ref={fileInput} id="pdf-file" type="file" accept={`.pdf,application/pdf,${imageAccept}`} disabled={working || !canUpload} onChange={e => { setFile(e.target.files?.[0] ?? null); setConsent(false) }} />
          <p>PDF、PNG、JPEG、WebP，单文件最多 10 MiB。当前工作空间内的会话共享资料库。</p>
          {!canUpload && <p>{readOnly ? '当前为只读权限，可查看资料与已有研究结果。' : '请启用真实 Embedding，并确认知识库允许写入。'}</p>}
          <label className="upload-consent"><input type="checkbox" checked={consent} disabled={working || readOnly} onChange={e => setConsent(e.target.checked)} />我同意将文字发送至向量服务；启用视觉识别或图像检索时也会发送页面图片，可能产生费用。</label>
          <button onClick={uploadPdf} disabled={working || !canUpload || !file || !consent}>{busy === 'upload' ? '正在提交…' : '上传并入库'}</button>
        </section>
        <label className="library-search"><Icon name="search" /><input aria-label="搜索文献名称" placeholder="查找文献…" value={filter} onChange={event => setFilter(event.target.value)} /></label>
        <div className="document-selection"><span>已选 {chosenIds.length} 篇</span><button disabled={asking || working} onClick={() => setSelectedIds(null)}>全选</button><button disabled={asking || working} onClick={() => setSelectedIds([])}>清空</button></div>
        <div className="document-list">
          {busy === 'loading' && <p className="library-hint">正在同步文献库…</p>}
          {!working && documents.length === 0 && <div className="library-empty"><Icon name="book" /><p>你的第一篇文献<br />从这里开始。</p><span>上传 PDF，开始围绕原文研究。</span></div>}
          {visibleDocuments.map(document => <article className="document" key={document.id}><input type="checkbox" aria-label={`检索 ${document.title}`} checked={chosenIds.includes(document.id)} disabled={asking || working || !selectable.some(doc => doc.id === document.id)} onChange={e => {
            setSelectedIds(e.target.checked ? [...chosenIds, document.id] : chosenIds.filter(id => id !== document.id))
          }} /><div>
            {libraryDocumentId(document.source_path) ? <button className="document-open" disabled={document.page_count < 1} title="打开原文" onClick={() => setViewer({ id: document.id, page: 1 })}>{document.title}</button> : <h3 title={document.title}>{document.title}</h3>}
            <p>{document.chunk_count} 个片段 {document.page_count > 0 && ` · ${document.page_count} 页`}<span>文献资料</span></p>
            <small className="document-status">{statusLabel[document.status] ?? document.status}</small>
            {document.analysis_mode === 'vision' && <small className="document-status">已识别图表与扫描页</small>}
            {!!document.image_indexed_pages && <small className="document-status">已索引 {document.image_indexed_pages} 页图像</small>}
            {document.error && <p className="document-error">{document.error}</p>}
            {document.warnings.map(warning => <p className="document-warning" key={warning}>{warning}</p>)}
            {['failed', 'not_indexed'].includes(document.status) && <button className="retry-document" disabled={working || !canUpload} onClick={() => retryDocument(document.id)}>重新处理（可能计费）</button>}
            {document.vision_available && document.status === 'ready' && document.analysis_mode !== 'vision' && <button className="retry-document" disabled={asking || working || !canUpload} onClick={() => retryDocument(document.id, true)}>识别图表与扫描页（可能计费）</button>}
            {document.image_index_available && ['ready', 'failed'].includes(document.status) && document.chunk_count > 0 && (document.image_indexed_pages ?? 0) < document.page_count && <button className="retry-document" disabled={asking || working || !canUpload} onClick={() => retryDocument(document.id, false, true)}>建立图像索引（可能计费）</button>}
          </div></article>)}
          {documents.length > 0 && visibleDocuments.length === 0 && <p className="library-hint">没有匹配的文献名称。</p>}
        </div>
        <div className="sidebar-bottom">
          <details className={`persistence-panel persistence-${storage.tone}`} aria-label="知识库保存状态"><summary>存储管理 · {storage.label}</summary>
            <div className="persistence-heading"><span>本地快照</span><span className="persistence-badge" role="status">{busy === 'save' ? '正在保存…' : busy === 'load' ? '正在加载…' : storage.label}</span></div>
            <p className="persistence-detail">{storage.detail}</p>
            <div className="persistence-actions">
              <button onClick={() => void persist('save')} disabled={readOnly || working || !storage.canSave}>保存知识库</button>
              <button onClick={requestReload} disabled={readOnly || working || !storage.canLoad}>重新加载</button>
            </div>
            {confirmReload && <div className="reload-confirm" role="alert"><p>重新加载会用磁盘快照替换当前内存，未保存内容将丢失。</p><button onClick={() => void persist('load')} disabled={working}>确认重新加载</button><button onClick={() => setConfirmReload(false)} disabled={working}>取消</button></div>}
          </details>
          {(storage.tone === 'error' || persistence?.write_blocked || persistenceIssue) && <p role="alert">{persistenceIssue || storage.detail}</p>}
          <button className="refresh-button" onClick={syncLibrary} disabled={working}><Icon name="refresh" className={busy === 'loading' ? 'spinning' : ''} />刷新文献</button><div className="connection"><span className={`connection-dot ${connection}`} />{connection === 'checking' ? '连接中' : connection === 'online' ? '已连接' : '未连接'}</div>
        </div>
      </aside>
  )
}
