import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../../shared/api'
import type { PaperDocument, PersistenceStatus } from '../../shared/api'
import { persistenceView } from './persistence'
import { validateImageFile } from '../../shared/imageFiles'

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
    visionEnabled: health.status === 'fulfilled' && !!health.value.vision_enabled,
    connectionError: health.status === 'rejected' ? messageOf(health.reason)
      : documents.status === 'rejected' ? messageOf(documents.reason) : '',
  }
}

export function useLibrary(readOnly = false) {
  const [mode, setMode] = useState<string | null>(null)
  const [visionEnabled, setVisionEnabled] = useState(false)
  const [documents, setDocuments] = useState<PaperDocument[]>([])
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState<'loading' | 'ingest' | 'upload' | 'ask' | 'save' | 'load' | null>('loading')
  const [file, setFile] = useState<File | null>(null)
  const [consent, setConsent] = useState(false)
  const [persistence, setPersistence] = useState<PersistenceStatus | null>(null)
  const [persistenceIssue, setPersistenceIssue] = useState('')
  const [confirmReload, setConfirmReload] = useState(false)
  const [connection, setConnection] = useState<'checking' | 'online' | 'unknown'>('checking')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const actionLock = useRef(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const hasPending = documents.some(doc => ['queued', 'parsing', 'analyzing', 'embedding'].includes(doc.status))

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
          setConnection(snapshot.connected ? 'online' : 'unknown'); setMode(snapshot.mode); setVisionEnabled(snapshot.visionEnabled)
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
    setConnection(snapshot.connected ? 'online' : 'unknown'); setMode(snapshot.mode); setVisionEnabled(snapshot.visionEnabled)
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
    const isPdf = file.name.toLowerCase().endsWith('.pdf')
    if ((isPdf && (!file.size || file.size > 10 * 1024 * 1024)) || (!isPdf && validateImageFile(file))) {
      setError('请选择非空 PDF、PNG、JPEG 或 WebP 文件，大小不超过 10 MiB。'); return
    }
    actionLock.current = true; setBusy('upload'); setError(''); setNotice('')
    try {
      const doc = await (isPdf ? api.uploadPdf(file) : api.uploadImage(file))
      setDocuments(items => [doc, ...items.filter(item => item.id !== doc.id)])
      setNotice(doc.status === 'ready' ? '这份资料已入库，未重复生成向量。' : '上传完成，正在后台处理。资料显示“可提问”后即可开始研究。')
      setFile(null); setConsent(false)
      if (fileInput.current) fileInput.current.value = ''
    } catch (e) { setError(`${messageOf(e)} 上传结果不确定时，请先刷新文献列表核对。`) }
    finally { await reconcileLibrary(); setBusy(null); actionLock.current = false }
  }

  async function retryDocument(id: string, analyze = false, images = false) {
    if (busy || actionLock.current) return
    actionLock.current = true; setBusy('upload'); setError('')
    try {
      const doc = await (images ? api.indexDocumentImages(id) : analyze ? api.analyzeDocument(id) : api.retryDocument(id))
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

  const working = busy !== null
  const ready = mode === 'live-llm-live-retrieval'
  const storage = persistenceView(persistence, persistenceIssue)
  const canUpload = !readOnly && storage.canIngest && ['live-llm-live-retrieval', 'live-retrieval-demo-llm'].includes(mode ?? '')
  return { mode, visionEnabled, documents, busy, file, setFile, consent, setConsent, filter, setFilter, fileInput, persistence, persistenceIssue, confirmReload, setConfirmReload, connection, error, notice, reconcileLibrary, syncLibrary, uploadPdf, retryDocument, persist, requestReload, working, ready, storage, canUpload }
}

export type LibraryController = ReturnType<typeof useLibrary>
