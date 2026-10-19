import Icon from '../shared/Icon'
import { useLibrary } from '../features/library/useLibrary'
import LibrarySidebar from '../features/library/LibrarySidebar'
import { useState } from 'react'
import Conversations from '../features/chat/Conversations'
import WikiPanel from '../features/wiki/WikiPanel'
import PdfViewer from '../features/evidence/PdfViewer'
import { libraryDocumentId } from '../features/evidence/pdfSource'

export default function App({ readOnly = false }: { readOnly?: boolean }) {
  const [view, setView] = useState<'research' | 'wiki'>('research')
  const [selectedIds, setSelectedIds] = useState<string[] | null>(null)
  const [asking, setAsking] = useState(false)
  const [viewer, setViewer] = useState<{ id: string; page: number } | null>(null)
  const library = useLibrary(readOnly)
  const { documents, visionEnabled, working, ready, canUpload, notice, error, syncLibrary, reconcileLibrary } = library

  const selectable = documents.filter(doc => doc.status === 'ready' && (doc.chunk_count > 0 || (doc.image_indexed_pages ?? 0) > 0) && libraryDocumentId(doc.source_path))
  const chosen = selectable.filter(doc => selectedIds === null || selectedIds.includes(doc.id))
  const chosenIds = chosen.map(doc => doc.id)
  const totalChunks = chosen.reduce((total, document) => total + document.chunk_count, 0)


  return (
    <div className="workspace">
      <a className="skip-link" href="#research-question">跳到问题输入</a>
      <LibrarySidebar library={library} readOnly={readOnly} asking={asking} chosenIds={chosenIds} selectable={selectable}
        setSelectedIds={setSelectedIds} setViewer={setViewer} />

      <div className="page">
        <header className="topbar"><span><Icon name="book" />文献研究</span><nav className="workspace-tabs" aria-label="工作区"><button type="button" aria-current={view === 'research' ? 'page' : undefined} onClick={() => setView('research')}>研究会话</button><button type="button" aria-current={view === 'wiki' ? 'page' : undefined} onClick={() => setView('wiki')}>Wiki</button></nav></header>
        <main>
          {!ready && !readOnly && <p className="notice">服务尚未就绪，请检查模型配置并重启后端。</p>}
          <div className="feedback" aria-live="polite">{notice && <p className="notice"><Icon name="check" />{notice}</p>}{error && <div className="error" role="alert"><span>{error}</span><button onClick={syncLibrary} disabled={working}>重新连接</button></div>}</div>

          {view === 'research' ? <><Conversations readOnly={readOnly} disabled={working} canAsk={!readOnly && ready && chosenIds.length > 0} canAttach={!readOnly && visionEnabled} canIndexAttachments={canUpload} onLibraryChange={reconcileLibrary} totalChunks={totalChunks} documentIds={chosenIds} onRestoreScope={setSelectedIds} onAskingChange={setAsking} onOpenSource={(id, page) => setViewer({ id, page })} /><p className="privacy-note">提问会将相关文字发送至模型服务；视觉证据也会发送原页图片，可能计费。请核对原文。</p></> : <WikiPanel readOnly={readOnly} documents={documents} documentIds={chosenIds} onOpenSource={(id, page) => setViewer({ id, page })} />}
        </main>
      </div>
      {viewer && <PdfViewer key={viewer.id + ':' + viewer.page} id={viewer.id} initialPage={viewer.page} onClose={() => setViewer(null)} />}
    </div>
  )
}
