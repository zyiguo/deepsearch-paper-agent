import type { PersistenceStatus } from '../../shared/api'

// Status is server-owned. A failed refresh must not reuse a stale "saved" badge.
export function persistenceView(status: PersistenceStatus | null, issue = '') {
  const controls = {
    canSave: status !== null && !status.write_blocked,
    canIngest: status !== null && !status.write_blocked,
    canLoad: status !== null && status.snapshot_exists,
  }
  if (!status) return { ...controls, label: '状态未知', tone: 'unknown', detail: issue || '正在读取保存状态…' }
  if (status.write_blocked) return { ...controls, label: '写入已保护', tone: 'error', detail: status.error || '快照加载异常。请修复文件后重新加载；当前内存仍可用于提问。' }
  if (status.error) return { ...controls, label: '持久化异常', tone: 'error', detail: status.error }
  if (!status.snapshot_exists) return { ...controls, label: '尚未保存', tone: 'pending', detail: '导入文献后自动保存，也可以手动创建快照。' }
  if (status.dirty) return { ...controls, label: '有未保存内容', tone: 'pending', detail: '当前内存尚未确认保存，请点击保存知识库。' }
  return { ...controls, label: '已保存到本地', tone: 'saved', detail: `最近确认 ${status.chunk_count} 个片段已保存。后端启动时自动恢复。` }
}
