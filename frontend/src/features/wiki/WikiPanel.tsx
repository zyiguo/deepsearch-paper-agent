import { useEffect, useState } from 'react'
import { api } from '../../shared/api'
import type { PaperDocument, WikiPage, WikiSummary } from '../../shared/api'
import './wiki.css'

type Props = {
  readOnly?: boolean
  documents: PaperDocument[]
  documentIds: string[]
  onOpenSource: (id: string, page: number) => void
}

export default function WikiPanel({ readOnly = false, documents, documentIds, onOpenSource }: Props) {
  const [pages, setPages] = useState<WikiSummary[]>([])
  const [selected, setSelected] = useState<WikiPage | null>(null)
  const [query, setQuery] = useState('')
  const [title, setTitle] = useState('')
  const [question, setQuestion] = useState('')
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function refresh(search = query) {
    try { setPages(await api.wikiPages(search)) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Wiki 暂不可用') }
  }

  useEffect(() => { void refresh('') }, [])

  async function open(id: string) {
    setError('')
    try { const page = await api.wikiPage(id); setSelected(page); setTitle(page.title); setQuestion(page.question); setEditing(false) }
    catch (cause) { setError(cause instanceof Error ? cause.message : '无法读取页面') }
  }

  async function compile() {
    if (busy || !title.trim() || !question.trim() || !documentIds.length) return
    setBusy(true); setError(''); setNotice('')
    try {
      const saved = await api.compileWiki({ title: title.trim(), question: question.trim(), document_ids: documentIds,
        ...(editing && selected ? { page_id: selected.id, expected_revision: selected.revision } : {}) })
      await refresh('')
      await open(saved.id)
      setNotice('已核验并保存，引用仍以 PDF 原文为准。')
    } catch (cause) { setError(cause instanceof Error ? cause.message : '编译失败') }
    finally { setBusy(false) }
  }

  async function remove() {
    if (!selected || busy || !window.confirm(`删除“${selected.title}”？`)) return
    setBusy(true); setError('')
    try { await api.deleteWiki(selected.id, selected.revision); setSelected(null); setEditing(false); await refresh(''); setNotice('页面已删除。') }
    catch (cause) { setError(cause instanceof Error ? cause.message : '删除失败') }
    finally { setBusy(false) }
  }

  const sourceDocument = (path: string) => documents.find(item => item.source_path === path)
  return <section className="wiki-workspace" aria-label="文献 Wiki">
    <div className="wiki-directory">
      <div className="wiki-directory-head"><h2>Wiki</h2><button type="button" title="新建页面" onClick={() => { setSelected(null); setEditing(false); setTitle(''); setQuestion(''); setError('') }}>＋</button></div>
      <form onSubmit={event => { event.preventDefault(); void refresh(query) }} className="wiki-search"><input aria-label="搜索 Wiki" placeholder="搜索主题" value={query} onChange={event => setQuery(event.target.value)} /><button type="submit">搜索</button></form>
      <div className="wiki-page-list">{pages.map(page => <button key={page.id} type="button" className={selected?.id === page.id ? 'selected' : ''} onClick={() => void open(page.id)}><strong>{page.title}</strong>{page.excerpt && <span>{page.excerpt}</span>}</button>)}</div>
      {!pages.length && <p className="wiki-empty">暂无页面</p>}
    </div>
    <div className="wiki-detail">
      <div className="wiki-detail-head"><h2>{selected && !editing ? selected.title : editing ? '更新页面' : '新建页面'}</h2>{selected && !editing && <div><button type="button" disabled={readOnly} onClick={() => setEditing(true)}>更新</button><button type="button" onClick={() => void remove()} disabled={busy || readOnly}>删除</button></div>}</div>
      {selected && !editing ? <>
        <p className="wiki-question">研究问题：{selected.question}</p>
        <article className="wiki-markdown">{selected.markdown}</article>
        <h3>原文依据</h3>
        <ul className="wiki-sources">{selected.sources.map(source => { const doc = sourceDocument(source.source_path); return <li key={source.chunk_id}>{doc ? <button type="button" onClick={() => onOpenSource(doc.id, source.page_number)}>{doc.title} · 第 {source.page_number} 页</button> : <span>{source.source_path} · 第 {source.page_number} 页</span>}<code>{source.chunk_id}</code></li> })}</ul>
        {(selected.related.length > 0 || selected.backlinks.length > 0) && <><h3>关联页面</h3><div className="wiki-links">{[...new Set([...selected.related, ...selected.backlinks])].map(id => <button key={id} type="button" onClick={() => void open(id)}>{pages.find(page => page.id === id)?.title ?? id}</button>)}</div></>}
      </> : <div className="wiki-editor"><label>标题<input maxLength={160} value={title} onChange={event => setTitle(event.target.value)} /></label><label>研究问题<textarea maxLength={2000} value={question} onChange={event => setQuestion(event.target.value)} rows={5} /></label><p>使用当前选中的 {documentIds.length} 篇文献编译。只有通过引用核验的结论会保存。</p><div><button type="button" onClick={() => void compile()} disabled={readOnly || busy || !documentIds.length || !title.trim() || !question.trim()}>{busy ? '核验中…' : editing ? '重新编译' : '编译页面'}</button>{editing && <button type="button" onClick={() => setEditing(false)}>取消</button>}</div></div>}
      {notice && <p className="wiki-notice" role="status">{notice}</p>}{error && <p className="wiki-error" role="alert">{error}</p>}
    </div>
  </section>
}
