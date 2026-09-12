import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from './api'
import type { PaperDocument, PersistenceStatus } from './api'
import { persistenceView } from './persistence'
import Conversations from './Conversations'
import PdfViewer from './PdfViewer'
import { pdfDocumentId } from './pdfSource'

type IconName = 'book' | 'plus' | 'refresh' | 'arrow' | 'file' | 'search' | 'check' | 'copy' | 'external'
const paths: Record<IconName, string> = {
  book: 'M12 7C8 4 5 4 2 5v14c3-1 6-1 10 2m0-14c4-3 7-3 10-2v14c-3-1-6-1-10 2V7',
  plus: 'M12 5v14M5 12h14', refresh: 'M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 14-2l1 6M4 12l1 6a8 8 0 0 0 14-2',
  arrow: 'M4 12h16m-6-6 6 6-6 6', file: 'M14 2H5v20h14V7l-5-5v5h5M8 12h8M8 16h6',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  check: 'm5 12 4 4L19 6', copy: 'M9 9h12v12H9zM15 5V3H3v12h2',
  external: 'M14 3h7v7M21 3l-9 9M10 3H3v18h18v-7',
}
function Icon({ name, className = '' }: { name: IconName; className?: string }) {
  return <svg className={`icon ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>
}
const messageOf = (error: unknown) => error instanceof Error ? error.message : '操作失败，请稍后重试。'

async function fetchLibrary(signal?: AbortSignal) {
  const [health, documents, persistence] = await Promise.allSettled([
    api.health(signal), api.documents(signal), api.persistenceStatus(signal),
  ])
  const issue = persistence.status === 'rejected'
    ? persistence.reason instanceof ApiError && persistence.reason.status === 404
      ? '后端尚未提供保存接口，请重启已更新的 FastAPI 服务。'
      : messageOf(persistence.reason)
    : ''
  return {
    items: documents.status === 'fulfilled' ? documents.value.filter(doc => !doc.source_path.startsWith('demo://')) : null,
    persistence: persistence.status === 'fulfilled' ? persistence.value : null,
    issue,
    mode: health.status === 'fulfilled' ? health.value.mode : null,
    retrievalMode: health.status === 'fulfilled' ? health.value.retrieval_mode ?? null : null,
    connected: health.status === 'fulfilled',
    connectionError: health.status === 'rejected' ? messageOf(health.reason)
      : documents.status === 'rejected' ? messageOf(documents.reason) : '',
  }
}

export default function App() {
  const [mode, setMode] = useState<string | null>(null)
  const [documents, setDocuments] = useState<PaperDocument[]>([])
  const [selectedIds, setSelectedIds] = useState<string[] | null>(null)
  const [asking, setAsking] = useState(false)
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState<'loading' | 'ingest' | 'upload' | 'ask' | 'save' | 'load' | null>('loading')
  const [file, setFile] = useState<File | null>(null)
  const [consent, setConsent] = useState(false)
  const [viewer, setViewer] = useState<{ id: string; page: number } | null>(null)
  const [persistence, setPersistence] = useState<PersistenceStatus | null>(null)
  const [persistenceIssue, setPersistenceIssue] = useState('')
  const [confirmReload, setConfirmReload] = useState(false)
  const [connection, setConnection] = useState<'checking' | 'online' | 'unknown'>('checking')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const actionLock = useRef(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const hasPending = documents.some(doc => ['queued', 'parsing', 'embedding'].includes(doc.status))

  useEffect(() => {
    if (!hasPending) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const snapshot = await fetchLibrary(AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]))
        if (controller.signal.aborted) return
        if (snapshot.items !== null) setDocuments(snapshot.items)
        setPersistence(snapshot.persistence); setPersistenceIssue(snapshot.issue)
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 2000)
      }
    }
    timer = setTimeout(poll, 1000)
    return () => { controller.abort(); clearTimeout(timer) }
  }, [hasPending])

  useEffect(() => {
    const controller = new AbortController()
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(15000)])
    fetchLibrary(signal)
      .then(snapshot => {
        if (!controller.signal.aborted) {
          if (snapshot.items !== null) setDocuments(snapshot.items)
          setPersistence(snapshot.persistence); setPersistenceIssue(snapshot.issue)
          setConnection(snapshot.connected ? 'online' : 'unknown'); setMode(snapshot.mode)
          setError(snapshot.connectionError)
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) { setError(messageOf(error)); setConnection('unknown') }
      })
      .finally(() => { if (!controller.signal.aborted) setBusy(null) })
    return () => controller.abort()
  }, [])


  async function reconcileLibrary() {
    const snapshot = await fetchLibrary()
    if (snapshot.items !== null) setDocuments(snapshot.items)
    setPersistence(snapshot.persistence); setPersistenceIssue(snapshot.issue)
    setConnection(snapshot.connected ? 'online' : 'unknown'); setMode(snapshot.mode)
    return snapshot
  }

  async function syncLibrary() {
    if (actionLock.current || busy) return
    actionLock.current = true
    setBusy('loading'); setError(''); setNotice(''); setConnection('checking'); setConfirmReload(false)
    try {
      const snapshot = await reconcileLibrary()
      if (snapshot.connectionError) setError(snapshot.connectionError)
      else setNotice(snapshot.issue ? '文献列表已更新，但保存状态暂不可用。' : '文献列表与保存状态已更新。')
    } catch (error) { setError(messageOf(error)); setConnection('unknown') }
    finally { setBusy(null); actionLock.current = false }
  }

  async function uploadPdf() {
    if (!file || !consent || busy || actionLock.current) return
    if (!file.name.toLowerCase().endsWith('.pdf') || file.size === 0 || file.size > 10 * 1024 * 1024) {
      setError('请选择非空 PDF 文件，大小不超过 10 MiB。'); return
    }
    actionLock.current = true; setBusy('upload'); setError(''); setNotice('')
    try {
      const doc = await api.uploadPdf(file)
      setDocuments(items => [doc, ...items.filter(item => item.id !== doc.id)])
      setNotice(doc.status === 'ready' ? '这份 PDF 已入库，未重复生成向量。' : '上传完成，正在后台处理。文献显示“可提问”后即可开始研究。')
      setFile(null); setConsent(false)
      if (fileInput.current) fileInput.current.value = ''
    } catch (e) { setError(`${messageOf(e)} 上传结果不确定时，请先刷新文献列表核对。`) }
    finally { await reconcileLibrary(); setBusy(null); actionLock.current = false }
  }

  async function retryDocument(id: string) {
    if (busy || actionLock.current) return
    actionLock.current = true; setBusy('upload'); setError('')
    try {
      const doc = await api.retryDocument(id)
      setDocuments(items => items.map(item => item.id === id ? doc : item))
      setNotice('已重新提交文献处理，可能再次调用向量服务。')
    } catch (e) { setError(messageOf(e)) }
    finally { setBusy(null); actionLock.current = false }
  }

  async function persist(action: 'save' | 'load') {
    const controls = persistenceView(persistence)
    if (actionLock.current || busy || (action === 'save' ? !controls.canSave : !controls.canLoad)) return
    actionLock.current = true
    setBusy(action); setError(''); setNotice(''); setConfirmReload(false)
    try {
      const response = action === 'save' ? await api.saveKnowledgeBase() : await api.loadKnowledgeBase()
      setNotice(response.message)
    } catch (error) { setError(messageOf(error)) }
    finally {
      // Do not infer "saved" from a click, nor retain an old green badge after a failed refresh.
      await reconcileLibrary()
      setBusy(null); actionLock.current = false
    }
  }

  function requestReload() {
    if (persistence?.dirty) setConfirmReload(true)
    else void persist('load')
  }

  const selectable = documents.filter(doc => doc.status === 'ready' && doc.chunk_count > 0 && pdfDocumentId(doc.source_path))
  const chosen = selectable.filter(doc => selectedIds === null || selectedIds.includes(doc.id))
  const chosenIds = chosen.map(doc => doc.id)
  const totalChunks = chosen.reduce((total, document) => total + document.chunk_count, 0)
  const visibleDocuments = documents.filter(document => document.title.toLowerCase().includes(filter.toLowerCase()))
  const working = busy !== null
  const ready = mode === 'live-llm-live-retrieval'
  const storage = persistenceView(persistence, persistenceIssue)
  const canUpload = storage.canIngest && ['live-llm-live-retrieval', 'live-retrieval-demo-llm'].includes(mode ?? '')
  const statusLabel: Record<string, string> = { queued: '排队中', parsing: '解析 PDF 中', embedding: '生成向量中', ready: '可提问', failed: '处理失败', not_indexed: '需重新入库' }

  return (
    <div className="workspace">
      <a className="skip-link" href="#research-question">跳到问题输入</a>
      <aside className="sidebar" aria-label="文献库">
        <a className="brand" href="/" aria-label="DeepSearch 首页"><span className="brand-symbol">D<span>·</span></span><span><strong>DeepSearch</strong></span></a>
        <div className="library-heading"><h2>文献库</h2><span className="count">{documents.length}</span></div>
        <section className="pdf-upload" aria-label="上传 PDF">
          <label htmlFor="pdf-file">上传 PDF</label>
          <input ref={fileInput} id="pdf-file" type="file" accept=".pdf,application/pdf" disabled={working || !canUpload} onChange={e => { setFile(e.target.files?.[0] ?? null); setConsent(false) }} />
          <p>仅文字 PDF，最多 10 MiB。所有会话共享文献。</p>
          {!canUpload && <p>请启用真实 Embedding，并确认知识库允许写入。</p>}
          <label className="upload-consent"><input type="checkbox" checked={consent} disabled={working} onChange={e => setConsent(e.target.checked)} />我同意将提取文字发送至已配置的向量服务，可能产生费用。</label>
          <button onClick={uploadPdf} disabled={working || !canUpload || !file || !consent}>{busy === 'upload' ? '正在提交…' : '上传并入库'}</button>
        </section>
        <label className="library-search"><Icon name="search" /><input aria-label="搜索文献名称" placeholder="查找文献…" value={filter} onChange={event => setFilter(event.target.value)} /></label>
        <div className="document-selection"><span>已选 {chosen.length} 篇</span><button disabled={asking || working} onClick={() => setSelectedIds(null)}>全选</button><button disabled={asking || working} onClick={() => setSelectedIds([])}>清空</button></div>
        <div className="document-list">
          {busy === 'loading' && <p className="library-hint">正在同步文献库…</p>}
          {!working && documents.length === 0 && <div className="library-empty"><Icon name="book" /><p>你的第一篇文献<br />从这里开始。</p><span>上传 PDF，开始围绕原文研究。</span></div>}
          {visibleDocuments.map(document => <article className="document" key={document.id}><input type="checkbox" aria-label={`检索 ${document.title}`} checked={chosenIds.includes(document.id)} disabled={asking || working || !selectable.some(doc => doc.id === document.id)} onChange={e => {
            setSelectedIds(e.target.checked ? [...chosenIds, document.id] : chosenIds.filter(id => id !== document.id))
          }} /><div>
            {pdfDocumentId(document.source_path) ? <button className="document-open" disabled={document.page_count < 1} title="打开原文" onClick={() => setViewer({ id: document.id, page: 1 })}>{document.title}</button> : <h3 title={document.title}>{document.title}</h3>}
            <p>{document.chunk_count} 个片段 {document.page_count > 0 && ` · ${document.page_count} 页`}<span>文献资料</span></p>
            <small className="document-status">{statusLabel[document.status] ?? document.status}</small>
            {document.error && <p className="document-error">{document.error}</p>}
            {document.warnings.map(warning => <p className="document-warning" key={warning}>{warning}</p>)}
            {['failed', 'not_indexed'].includes(document.status) && <button className="retry-document" disabled={working || !canUpload} onClick={() => retryDocument(document.id)}>重新处理（可能计费）</button>}
          </div></article>)}
          {documents.length > 0 && visibleDocuments.length === 0 && <p className="library-hint">没有匹配的文献名称。</p>}
        </div>
        <div className="sidebar-bottom">
          <details className={`persistence-panel persistence-${storage.tone}`} aria-label="知识库保存状态"><summary>存储管理 · {storage.label}</summary>
            <div className="persistence-heading"><span>本地快照</span><span className="persistence-badge" role="status">{busy === 'save' ? '正在保存…' : busy === 'load' ? '正在加载…' : storage.label}</span></div>
            <p className="persistence-detail">{storage.detail}</p>
            <div className="persistence-actions">
              <button onClick={() => void persist('save')} disabled={working || !storage.canSave}>保存知识库</button>
              <button onClick={requestReload} disabled={working || !storage.canLoad}>重新加载</button>
            </div>
            {confirmReload && <div className="reload-confirm" role="alert"><p>重新加载会用磁盘快照替换当前内存，未保存内容将丢失。</p><button onClick={() => void persist('load')} disabled={working}>确认重新加载</button><button onClick={() => setConfirmReload(false)} disabled={working}>取消</button></div>}
          </details>
          {(storage.tone === 'error' || persistence?.write_blocked || persistenceIssue) && <p role="alert">{persistenceIssue || storage.detail}</p>}
          <button className="refresh-button" onClick={syncLibrary} disabled={working}><Icon name="refresh" className={busy === 'loading' ? 'spinning' : ''} />刷新文献</button><div className="connection"><span className={`connection-dot ${connection}`} />{connection === 'checking' ? '连接中' : connection === 'online' ? '已连接' : '未连接'}</div>
        </div>
      </aside>

      <div className="page">
        <header className="topbar"><span><Icon name="book" />文献问答</span></header>
        <main>
          {!ready && <p className="notice">服务尚未就绪，请检查模型配置并重启后端。</p>}
          <div className="feedback" aria-live="polite">{notice && <p className="notice"><Icon name="check" />{notice}</p>}{error && <div className="error" role="alert"><span>{error}</span><button onClick={syncLibrary} disabled={working}>重新连接</button></div>}</div>

          <Conversations disabled={working} canAsk={ready && chosenIds.length > 0} totalChunks={totalChunks} documentIds={chosenIds} onRestoreScope={setSelectedIds} onAskingChange={setAsking} onOpenSource={(id, page) => setViewer({ id, page })} />
          <p className="privacy-note">提问会将相关文字发送至模型服务，可能计费。请核对原文。</p>
        </main>
      </div>
      {viewer && <PdfViewer key={viewer.id + ':' + viewer.page} id={viewer.id} initialPage={viewer.page} onClose={() => setViewer(null)} />}
    </div>
  )
}
