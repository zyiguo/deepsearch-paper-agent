import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError } from './api'
import type { Session, SessionDetail, Source, SessionMemory, ImageAttachment } from './api'
import './conversations.css'
import EvidenceSource, { sourcePageLabel } from './EvidenceSource'
import ImageAttachments from './ImageAttachments'
import SkillOptions from './SkillOptions'
import { imageAccept, validateImageFile } from './imageFiles'

const selectionKey = 'deepsearch.active-session'
function remember(id: string) { try { localStorage.setItem(selectionKey, id) } catch { /* Storage can be disabled. */ } }
function remembered() { try { return localStorage.getItem(selectionKey) } catch { return null } }
const message = (e: unknown) => e instanceof ApiError && e.status === 404
  ? '会话不存在或接口尚未加载，请刷新会话；若持续出现，请重启后端。'
  : e instanceof Error ? e.message : '操作失败，请重试。'

export default function Conversations({ disabled, canAsk = true, canAttach = false, canIndexAttachments = false, onLibraryChange, totalChunks, documentIds, onRestoreScope, onAskingChange, onOpenSource }: { disabled: boolean; canAsk?: boolean; canAttach?: boolean; canIndexAttachments?: boolean; onLibraryChange?: () => Promise<unknown>; totalChunks: number; documentIds: string[]; onRestoreScope: (ids: string[] | null) => void; onAskingChange: (asking: boolean) => void; onOpenSource: (id: string, page: number) => void }) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [active, setActive] = useState<SessionDetail | null>(null)
  const [title, setTitle] = useState('')
  const [question, setQuestion] = useState('')
  const [attachments, setAttachments] = useState<ImageAttachment[]>([])
  const [indexedAttachments, setIndexedAttachments] = useState<string[]>([])
  const [attachmentNotice, setAttachmentNotice] = useState('')
  const [topK, setTopK] = useState(3)
  const [skillIds, setSkillIds] = useState<string[] | undefined>(undefined)
  const [busy, setBusy] = useState<string | null>('加载会话')
  const [error, setError] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const [copied, setCopied] = useState<number | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [memory, setMemory] = useState<SessionMemory | null>(null)
  const [goal, setGoal] = useState('')
  const [language, setLanguage] = useState('zh')
  const [style, setStyle] = useState('简洁')
  const [confirmClear, setConfirmClear] = useState(false)
  function showMemory(value: SessionMemory | null) {
    setMemory(value); setGoal(value?.research_goal ?? '')
    setLanguage(value?.language ?? 'zh'); setStyle(value?.answer_style ?? '简洁'); setConfirmClear(false)
  }
  const [stage, setStage] = useState('准备中')
  const [pending, setPending] = useState<{ question: string; text: string; sources: Source[] } | null>(null)
  const streamController = useRef<AbortController | null>(null)
  const lock = useRef(false)
  const end = useRef<HTMLDivElement>(null)
  const scopeAttempt = useRef('')
  const scopeKey = JSON.stringify([...documentIds].sort())
  useEffect(() => {
    if (!active || busy || disabled) return
    const key = active.id + ':' + scopeKey
    if (scopeKey === JSON.stringify([...(memory?.document_ids ?? [])].sort()) && memory?.document_ids !== null && memory) return
    if (scopeAttempt.current === key) return
    scopeAttempt.current = key
    void run('更新文献范围', async () => showMemory(await api.setMemoryScope(active.id, documentIds)))
  }, [active?.id, scopeKey, busy, disabled, memory?.document_ids])
  useEffect(() => { onAskingChange(busy === '生成回答'); return () => onAskingChange(false) }, [busy, onAskingChange])
  useEffect(() => () => streamController.current?.abort(), [])

  useEffect(() => {
    if (busy !== '生成回答') return
    const started = Date.now()
    setElapsed(0)
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000)
    return () => clearInterval(timer)
  }, [busy])

  useEffect(() => {
    const controller = new AbortController()
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(15000)])
    async function restore() {
      try {
        const items = await api.sessions(signal)
        const id = items.find(item => item.id === remembered())?.id ?? items[0]?.id
        const detail = id ? await api.session(id, signal) : null
        if (controller.signal.aborted) return
        setSessions(items); setActive(detail); setAttachments(detail?.turns.at(-1)?.attachments ?? [])
        showMemory(detail?.memory ?? null); onRestoreScope(detail?.memory?.document_ids ?? null)
        if (detail) remember(detail.id)
      } catch (e) { if (!controller.signal.aborted) setError(message(e)) }
      finally { if (!controller.signal.aborted) setBusy(null) }
    }
    void restore()
    return () => controller.abort()
  }, [])

  useEffect(() => { if (active?.turns.length) end.current?.scrollIntoView({ block: 'nearest' }) }, [active?.id, active?.turns.length])
  useEffect(() => { if (pending) end.current?.scrollIntoView({ block: 'nearest' }) }, [pending?.question])

  async function run(label: string, action: () => Promise<void>) {
    if (lock.current || busy || disabled) return
    lock.current = true; setBusy(label); setError('')
    try { await action() } catch (e) { setError(message(e)) }
    finally { lock.current = false; setBusy(null) }
  }

  function refresh() {
    setConfirmDelete(false)
    void run('刷新会话', async () => {
      const items = await api.sessions()
      const id = items.find(item => item.id === active?.id)?.id ?? items[0]?.id
      const detail = id ? await api.session(id) : null
      setSessions(items); setActive(detail); setUncertain(false); setAttachments(detail?.turns.at(-1)?.attachments ?? [])
      showMemory(detail?.memory ?? null); onRestoreScope(detail?.memory?.document_ids ?? null)
      if (detail) remember(detail.id)
    })
  }

  function create(event: FormEvent) {
    event.preventDefault()
    setConfirmDelete(false)
    if (!title.trim()) return
    void run('新建会话', async () => {
      const session = await api.createSession(title.trim())
      setSessions(items => [session, ...items]); setActive({ ...session, turns: [] })
      setAttachments([]); setIndexedAttachments([]); setAttachmentNotice('')
      showMemory(null)
      remember(session.id); setTitle(''); setQuestion(''); setUncertain(false)
    })
  }

  function select(id: string) {
    setConfirmDelete(false)
    void run('切换会话', async () => {
      const detail = await api.session(id)
      setActive(detail); remember(id); setQuestion(''); setUncertain(false)
      setAttachments(detail.turns.at(-1)?.attachments ?? []); setIndexedAttachments([]); setAttachmentNotice('')
      showMemory(detail.memory ?? null); onRestoreScope(detail.memory?.document_ids ?? null)
    })
  }

  function removeActive() {
    if (!active) return
    const id = active.id
    void run('删除会话', async () => {
      try { await api.deleteSession(id) }
      catch (e) { if (!(e instanceof ApiError && e.status === 404)) throw e }
      setSessions(items => items.filter(item => item.id !== id))
      setActive(null); setQuestion(''); setUncertain(false); setConfirmDelete(false)
      setAttachments([]); setIndexedAttachments([]); setAttachmentNotice('')
      showMemory(null)
      try { localStorage.removeItem(selectionKey) } catch { /* Optional browser storage. */ }
    })
  }

  function send(event: FormEvent) {
    event.preventDefault()
    if (!active || !question.trim() || uncertain || !(canAsk || (canAttach && attachments.length > 0))) return
    const session = active
    const submitted = question.trim()
    void run('生成回答', async () => {
      const controller = new AbortController()
      streamController.current = controller
      setStage('准备中'); setPending({ question: submitted, text: '', sources: [] })
      try {
        const turn = await api.streamTurn(session.id, submitted, topK, event => {
          if (controller.signal.aborted) return
          if (event.type === 'status') {
            const labels = { queued: '等待处理', planning: '正在规划', retrieving: '正在检索', drafting: '正在整理回答', verifying: '正在核验', revising: '正在补充修订', summarizing: '正在整理会话记忆' }
            setStage(labels[event.stage])
          } else setPending(value => value ? { ...value, text: value.text + event.text, sources: event.sources } : value)
        }, controller.signal, documentIds, attachments.map(image => image.id), skillIds)
        setActive({ ...session, turns: [...session.turns, turn], updated_at: turn.created_at })
        setSessions(items => [{ ...session, updated_at: turn.created_at }, ...items.filter(item => item.id !== session.id)])
        setQuestion('')
        // An optional memory refresh failure must not make a saved turn uncertain.
        try { showMemory(await api.memory(session.id)) } catch { setError('回答已保存，记忆状态暂未刷新。') }
      } catch (e) {
        // A timeout can happen after the server saved the turn. Never automatically resend.
        setUncertain(true)
        throw e
      } finally {
        streamController.current = null
        setPending(null)
      }
    })
  }

  const blocked = disabled || busy !== null
  const questionAllowed = canAsk || (canAttach && attachments.length > 0)

  function uploadImages(files: File[]) {
    if (!active || !canAttach || blocked || uncertain || !files.length) return
    if (attachments.length + files.length > 4) { setError('每次提问最多附加 4 张图片。'); return }
    for (const file of files) {
      const issue = validateImageFile(file)
      if (issue) { setError(issue); return }
    }
    const session = active.id
    void run('上传图片', async () => {
      setAttachmentNotice('')
      for (const file of files) {
        const image = await api.uploadAttachment(session, file)
        setAttachments(items => [...items.filter(item => item.id !== image.id), image])
      }
    })
  }

  function indexImage(id: string) {
    if (!active || !canIndexAttachments) return
    const session = active.id
    void run('图片入库', async () => {
      const doc = await api.indexAttachment(session, id)
      setIndexedAttachments(items => [...items, id])
      setAttachmentNotice(doc.status === 'ready' ? '图片已在资料库中。' : '图片已提交入库，正在后台处理。')
      await onLibraryChange?.()
    })
  }

  function reuseImage(image: ImageAttachment) {
    if (attachments.some(item => item.id === image.id)) return
    if (attachments.length >= 4) { setError('每次提问最多附加 4 张图片。'); return }
    setAttachments(items => [...items, image])
  }
  return <section className="conversation-workspace" aria-label="研究会话">
    <aside className="session-panel panel" aria-label="会话列表">
      <div className="session-heading"><h2>研究会话</h2><span>{sessions.length}</span></div>
      <form onSubmit={create} className="session-create">
        <label htmlFor="session-title">新会话名称</label>
        <input id="session-title" placeholder="例如：训练方法研究" value={title} maxLength={100} onChange={e => setTitle(e.target.value)} disabled={blocked} required />
        <button className="primary-button" disabled={blocked || !title.trim()}>＋ 新建会话</button>
      </form>
      <nav className="session-list" aria-label="选择研究会话">{sessions.map(session => <button key={session.id} aria-current={active?.id === session.id ? 'true' : undefined} disabled={blocked} onClick={() => select(session.id)}><strong>{session.title}</strong><small>{new Date(session.updated_at).toLocaleString('zh-CN')}</small></button>)}</nav>
      {!sessions.length && <p className="session-hint">创建一个会话，开始连续研究。</p>}
      <button className="session-refresh" onClick={refresh} disabled={blocked}>刷新会话与历史</button>
      {active && <button className="session-refresh" onClick={() => setConfirmDelete(true)} disabled={blocked}>删除当前会话</button>}
      {confirmDelete && active && <div className="reload-confirm" role="alert"><p>删除“{active.title}”及其全部问答记录？此操作不可撤销，不会删除 PDF 或知识库。</p><button onClick={removeActive} disabled={blocked}>确认删除</button><button onClick={() => setConfirmDelete(false)} disabled={blocked}>取消</button></div>}
    </aside>
    <div className="chat-panel">
      <div className="result-heading"><h2>{active?.title ?? '开启一段研究'}</h2><span>{active ? `${active.turns.length} 轮问答` : '尚未选择会话'}</span></div>
      {active && <details className="memory-panel"><summary>记忆{memory?.compression_status === 'failed' ? ' · 压缩失败' : memory?.summary ? ' · 已更新' : ''}</summary>
        <p className="session-hint">只辅助理解，不作为论文证据。超过 6 轮后分批压缩，可能产生模型费用。</p>
        <label>研究目标<input value={goal} maxLength={1000} disabled={blocked} onChange={e => setGoal(e.target.value)} /></label>
        <div className="memory-preferences"><label>语言<input value={language} maxLength={40} disabled={blocked} onChange={e => setLanguage(e.target.value)} /></label><label>回答风格<input value={style} maxLength={100} disabled={blocked} onChange={e => setStyle(e.target.value)} /></label></div>
        <button disabled={blocked} onClick={() => void run('保存记忆设置', async () => showMemory(await api.editMemory(active.id, { research_goal: goal, language, answer_style: style })))}>保存设置</button>
        <p className="memory-summary">{memory?.summary || '暂无压缩摘要'}</p>
        {!!memory?.active_entities.length && <p>讨论对象：{memory.active_entities.join('、')}</p>}
        {!!memory?.open_questions.length && <p>待解决：{memory.open_questions.join('；')}</p>}
        {memory?.compression_status === 'failed' && <p role="status">保留上次摘要，新问题仍可正常回答；不会重试同一批。</p>}
        <button disabled={blocked} onClick={() => setConfirmClear(true)}>清空记忆</button>
        {confirmClear && <div role="alert"><p>清空摘要与近期上下文？历史记录、研究目标、偏好和文献选择保留。</p><button disabled={blocked} onClick={() => void run('清空记忆', async () => showMemory(await api.clearMemory(active.id)))}>确认清空</button><button onClick={() => setConfirmClear(false)}>取消</button></div>}
      </details>}
      {error && <p className="error" role="alert">{error}</p>}
      {uncertain && <p className="notice">发送未获确认。请先点击“刷新会话与历史”，核对是否已经保存，再决定是否重发。</p>}
      <div className="chat-history" aria-label="问答历史" aria-busy={busy !== null}>
        {!active?.turns.length && !pending && <div className="empty-state"><h3>{active ? '向文献提问' : '新建或选择一个会话'}</h3></div>}
        {active?.turns.map((turn, index) => <article className="chat-turn" key={turn.id}>
          <div className="turn-question"><span>第 {index + 1} 轮 · 你</span><h3>{turn.question}</h3></div>
          <ImageAttachments attachments={turn.attachments ?? []} disabled={blocked || uncertain} onReuse={canAttach ? reuseImage : undefined} />
          <div className="answer-panel panel"><div className="answer-title"><span className="answer-label">研究助手 · 已保存回答</span><button className="copy-button" onClick={async () => {
            try {
              await navigator.clipboard.writeText([turn.question, turn.answer.text, ...turn.answer.sources.map(source => `[${source.citation_id}] ${source.source_path} · ${sourcePageLabel(source)}\n${source.section_path?.join(' > ') ?? ''}\n${source.text}`)].join('\n\n'))
              setCopied(turn.id)
            } catch { setError('复制失败，请选中回答文字手动复制。') }
          }}>{copied === turn.id ? '已复制' : '复制回答'}</button></div><p className="answer-text">{turn.answer.text}</p>
          {!!turn.answer.skills_used?.length && <p className="used-skills">本轮技能：{turn.answer.skills_used.map(skill => skill.name).join('、')}</p>}
            <details className="turn-evidence" open><summary>本轮证据 · {turn.answer.sources.length} 条</summary>
              {!turn.answer.sources.length && <p className="session-hint">暂无可展示的引用。</p>}
              {turn.answer.sources.map(source => <EvidenceSource key={source.citation_id} source={source} onOpenSource={onOpenSource} />)}
            </details>
          </div>
        </article>)}
        {pending && <article className="chat-turn pending-turn">
          <div className="turn-question"><span>你</span><h3>{pending.question}</h3></div>
          <div className="answer-panel panel"><div className="stream-status" role="status" aria-live="polite"><span className="loading-ring" aria-hidden="true" />{stage}<small>{elapsed} 秒</small></div>
            {pending.text && <><p className="answer-text">{pending.text}</p>{pending.sources.map(source => <EvidenceSource key={source.citation_id} source={source} onOpenSource={onOpenSource} />)}</>}
          </div>
        </article>}
        <div ref={end} />
      </div>
      <p className="chat-status" role="status">{busy && busy !== '生成回答' ? `${busy}…` : ''}</p>
      <form className="question-panel panel" onSubmit={send}>
        <div className="panel-heading"><label htmlFor="research-question">继续提问</label><span className="scope-hint">{documentIds.length ? `已选 ${documentIds.length} 份资料` : attachments.length ? '仅使用本次附件' : '未选择资料'}</span></div>
        <ImageAttachments attachments={attachments} disabled={blocked || uncertain} indexed={indexedAttachments} onRemove={id => setAttachments(items => items.filter(item => item.id !== id))} onIndex={canIndexAttachments ? indexImage : undefined} />
        {attachmentNotice && <p className="attachment-notice" role="status">{attachmentNotice}</p>}
        <label className="attachment-upload">附图 · {attachments.length}/4
          <input aria-label="添加提问图片" type="file" accept={imageAccept} multiple disabled={!canAttach || blocked || !active || uncertain || attachments.length >= 4} onChange={e => { uploadImages(Array.from(e.target.files ?? [])); e.target.value = '' }} />
        </label>
        <textarea id="research-question" value={question} onChange={e => setQuestion(e.target.value)} placeholder="围绕资料或附图提问…" maxLength={2000} required disabled={blocked || (!canAsk && !canAttach) || !active || uncertain} onPaste={event => {
          const images = Array.from(event.clipboardData.files).filter(file => file.type.startsWith('image/'))
          if (images.length && canAttach) { event.preventDefault(); uploadImages(images) }
        }} />
        <div className="form-footer"><details className="advanced-options"><summary>选项</summary><label className="top-k">检索片段数<select value={topK} onChange={e => setTopK(Number(e.target.value))} disabled={blocked}>{[1, 2, 3, 5, 10].map(k => <option key={k}>{k}</option>)}</select><span className="available">共 {totalChunks} 个片段</span></label><SkillOptions value={skillIds} onChange={setSkillIds} disabled={blocked} /></details><button className="primary-button" disabled={blocked || !questionAllowed || !active || !question.trim() || uncertain}>{busy === '生成回答' ? '回答中…' : '发送 →'}</button></div>
      </form>
    </div>
  </section>
}
