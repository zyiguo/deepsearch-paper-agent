import { z } from 'zod'
import { imageContentType } from './imageFiles'

const documentSchema = z.object({
  id: z.string(), title: z.string(), source_path: z.string(),
  chunk_count: z.number().int().nonnegative(),
  status: z.string().default('ready'), page_count: z.number().int().nonnegative().default(0),
  error: z.string().nullable().default(null), warnings: z.array(z.string()).default([]),
  file_url: z.string().nullable().default(null),
  vision_available: z.boolean().optional(), analysis_mode: z.enum(['text', 'vision']).optional(),
  image_index_available: z.boolean().optional(), image_indexed_pages: z.number().int().nonnegative().optional(),
  media_type: z.string().optional(),
})
const attachmentSchema = z.object({
  id: z.string(), title: z.string(), source_path: z.string(), image_url: z.string(),
  width: z.number().int().positive(), height: z.number().int().positive(),
})
export type ImageAttachment = z.infer<typeof attachmentSchema>
const sourceSchema = z.object({
  citation_id: z.number().int().positive(),
  source_path: z.string(), page_number: z.number().int().positive(), text: z.string(),
  page_end: z.number().int().positive().nullable().optional(),
  section_path: z.array(z.string()).optional(),
  pages: z.array(z.object({ page_number: z.number().int().positive(), text: z.string() })).optional(),
  evidence_kind: z.enum(['text', 'ocr', 'figure', 'table', 'formula', 'image']).optional(),
  visual_pages: z.array(z.number().int().positive()).optional(),
  chunk_id: z.string().optional(), content_id: z.string().optional(), asset_ids: z.array(z.string()).optional(),
})
const answerSchema = z.object({
  mode: z.string(), text: z.string(), sources: z.array(sourceSchema),
  skills_used: z.array(z.object({ name: z.string(), sha256: z.string() })).optional(),
})
const persistenceSchema = z.object({
  snapshot_exists: z.boolean(), chunk_count: z.number().int().nonnegative(),
  dirty: z.boolean(), write_blocked: z.boolean(), error: z.string().nullable(),
})
export type PersistenceStatus = z.infer<typeof persistenceSchema>
export type PaperDocument = z.infer<typeof documentSchema>
export type Answer = z.infer<typeof answerSchema>
export type Source = z.infer<typeof sourceSchema>
const sessionSchema = z.object({ id: z.string(), title: z.string(), created_at: z.string(), updated_at: z.string() })
const turnSchema = z.object({ id: z.number().int(), question: z.string(), created_at: z.string(), answer: answerSchema.omit({ mode: true }), attachments: z.array(attachmentSchema).optional() })
const memorySchema = z.object({
  version: z.number(), revision: z.number(), document_ids: z.array(z.string()).nullable(),
  research_goal: z.string(), language: z.string(), answer_style: z.string(), summary: z.string(),
  active_entities: z.array(z.string()), open_questions: z.array(z.string()),
  compression_status: z.enum(['idle', 'ready', 'failed', 'pending']),
  summary_through_turn: z.number(),
})
export type SessionMemory = z.infer<typeof memorySchema>
const sessionDetailSchema = sessionSchema.extend({ turns: z.array(turnSchema), memory: memorySchema.nullable().optional() })
export type Session = z.infer<typeof sessionSchema>
export type SessionDetail = z.infer<typeof sessionDetailSchema>
export type Turn = z.infer<typeof turnSchema>
const statusEvent = z.object({ stage: z.enum(['queued', 'planning', 'retrieving', 'drafting', 'verifying', 'revising', 'summarizing']) })
const answerEvent = z.object({ text: z.string(), sources: z.array(sourceSchema) })
export type StreamUpdate = ({ type: 'status' } & z.infer<typeof statusEvent>) | ({ type: 'answer' } & z.infer<typeof answerEvent>)

async function streamTurn(id: string, question: string, topK: number, onEvent: (event: StreamUpdate) => void, signal?: AbortSignal, documentIds?: string[], attachmentIds?: string[], skillIds?: string[]): Promise<Turn> {
  const timeout = AbortSignal.timeout(600000)
  const response = await fetch(`/api/sessions/${encodeURIComponent(id)}/turns/stream`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ question, top_k: topK, document_ids: documentIds, attachment_ids: attachmentIds, skill_ids: skillIds }), signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  })
  if (!response.ok) throw new ApiError(detailMessage(await response.json().catch(() => null)) ?? '无法开始回答。', response.status)
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new ApiError('接口未提供流式响应，请重启后端。')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      buffer = buffer.replace(/\r\n/g, '\n')
      let boundary: number
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2)
        const lines = frame.split('\n')
        const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim()
        if (!event) continue // Heartbeat.
        const data = JSON.parse(lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n'))
        if (event === 'status') onEvent({ type: 'status', ...statusEvent.parse(data) })
        else if (event === 'answer') onEvent({ type: 'answer', ...answerEvent.parse(data) })
        else if (event === 'done') return turnSchema.parse(data)
        else if (event === 'error') throw new ApiError(z.object({ message: z.string() }).parse(data).message)
        else throw new ApiError('未知流式事件，请检查前后端版本。')
      }
      if (buffer.length > 2000000) throw new ApiError('流式数据异常。')
      if (done) throw new ApiError('连接中断，尚未确认回答已保存。请刷新会话核对，勿重复发送。')
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

export class ApiError extends Error {
  constructor(message: string, public readonly status?: number) {
    super(message)
    this.name = 'ApiError'
  }
}

function detailMessage(body: unknown): string | undefined {
  if (!body || typeof body !== 'object' || !('detail' in body)) return
  const detail = body.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((entry: unknown) => {
      if (entry && typeof entry === 'object' && 'msg' in entry) return String(entry.msg)
      return '请求参数不符合要求'
    }).join('；')
  }
}

async function request<T>(
  path: string, schema: z.ZodType<T, z.ZodTypeDef, unknown>, options: RequestInit = {}, timeoutMs = 15000,
): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...options, signal: options.signal ?? AbortSignal.timeout(timeoutMs),
    })
  } catch (error) {
    if (error instanceof Error && error.name === 'TimeoutError') {
      throw new ApiError('等待响应超时。后端可能仍在处理，请刷新会话历史核对结果，不要直接重复发送。')
    }
    if (error instanceof Error && error.name === 'AbortError') throw error
    throw new ApiError('暂时无法连接后端，或请求已超时。请确认 FastAPI 正在 8000 端口运行，然后重试。')
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    throw new ApiError(
      detailMessage(body) ?? `服务暂时不可用（HTTP ${response.status}），请检查后端后重试。`,
      response.status,
    )
  }
  const result = schema.safeParse(body)
  if (!result.success) throw new ApiError('接口返回的数据格式不符合约定，请检查前后端版本。')
  return result.data
}

export const api = {
  skills: () => request('/api/skills', z.object({ skills: z.array(z.object({ name: z.string(), description: z.string() })), warnings: z.array(z.string()), max_active: z.number(), automatic: z.boolean() })),
  uploadImage: (file: File) => request(`/api/documents/upload-image?filename=${encodeURIComponent(file.name)}`, documentSchema, {
    method: 'POST', headers: { 'Content-Type': imageContentType(file) }, body: file,
  }, 90000),
  uploadAttachment: (session: string, file: File) => request(`/api/sessions/${encodeURIComponent(session)}/attachments?filename=${encodeURIComponent(file.name)}`, attachmentSchema, {
    method: 'POST', headers: { 'Content-Type': imageContentType(file) }, body: file,
  }, 90000),
  indexAttachment: (session: string, id: string) => request(`/api/sessions/${encodeURIComponent(session)}/attachments/${encodeURIComponent(id)}/index`, documentSchema, { method: 'POST' }),
  memory: (id: string) => request(`/api/sessions/${encodeURIComponent(id)}/memory`, memorySchema),
  setMemoryScope: (id: string, document_ids: string[]) => request(`/api/sessions/${encodeURIComponent(id)}/memory/scope`, memorySchema, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ document_ids }),
  }),
  editMemory: (id: string, values: { research_goal: string; language: string; answer_style: string }) => request(`/api/sessions/${encodeURIComponent(id)}/memory`, memorySchema, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values),
  }),
  clearMemory: (id: string) => request(`/api/sessions/${encodeURIComponent(id)}/memory`, memorySchema, { method: 'DELETE' }),
  streamTurn,
  uploadPdf: (file: File) => request(`/api/documents/upload?filename=${encodeURIComponent(file.name)}`, documentSchema, {
    method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: file,
  }, 90000),
  retryDocument: (id: string) => request(`/api/documents/${encodeURIComponent(id)}/retry`, documentSchema, { method: 'POST' }),
  analyzeDocument: (id: string) => request(`/api/documents/${encodeURIComponent(id)}/analyze`, documentSchema, { method: 'POST' }),
  indexDocumentImages: (id: string) => request(`/api/documents/${encodeURIComponent(id)}/index-images`, documentSchema, { method: 'POST' }),
  documentPage: (id: string, page: number, signal?: AbortSignal) => request(`/api/documents/${encodeURIComponent(id)}/pages/${page}`, z.object({
    id: z.string(), title: z.string(), page_number: z.number().int().positive(), page_count: z.number().int().positive(), text: z.string(),
    media_type: z.string().optional(),
  }), { signal }),
  sessions: (signal?: AbortSignal) => request('/api/sessions', z.array(sessionSchema), { signal }),
  deleteSession: (id: string) => request(`/api/sessions/${encodeURIComponent(id)}`, z.object({ message: z.string() }), { method: 'DELETE' }),
  session: (id: string, signal?: AbortSignal) => request(`/api/sessions/${encodeURIComponent(id)}`, sessionDetailSchema, { signal }),
  createSession: (title: string) => request('/api/sessions', sessionSchema, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }),
  }),
  sendTurn: (id: string, question: string, topK: number) => request(`/api/sessions/${encodeURIComponent(id)}/turns`, turnSchema, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question, top_k: topK }),
  }, 330000),
  health: (signal?: AbortSignal) => request('/api/health', z.object({
    status: z.literal('ok'), mode: z.string(),
    retrieval_mode: z.enum(['semantic', 'bm25', 'hybrid']).optional(),
    vision_enabled: z.boolean().optional(),
    image_retrieval_enabled: z.boolean().optional(),
  }), { signal }),
  documents: (signal?: AbortSignal) => request('/api/documents', z.array(documentSchema), { signal }),
  persistenceStatus: (signal?: AbortSignal) => request('/api/knowledge-base/status', persistenceSchema, { signal }),
  saveKnowledgeBase: () => request('/api/knowledge-base/save', z.object({ message: z.string() }), { method: 'POST' }),
  loadKnowledgeBase: () => request('/api/knowledge-base/load', z.object({ message: z.string() }), { method: 'POST' }),
  ingestDemo: () => request('/api/demo/ingest', z.object({
    added_chunks: z.number().int().nonnegative(), message: z.string(),
  }), { method: 'POST' }, 90000),
  ask: (question: string, topK: number) => request('/api/ask', answerSchema, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, top_k: topK }),
  }, 330000),
}
