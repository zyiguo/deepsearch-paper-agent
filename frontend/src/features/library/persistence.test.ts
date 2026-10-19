import { describe, expect, it } from 'vitest'
import { persistenceView } from './persistence'
import type { PersistenceStatus } from '../../shared/api'

const saved: PersistenceStatus = { snapshot_exists: true, chunk_count: 2, dirty: false, write_blocked: false, error: null }

describe('persistence controls', () => {
  it('enables reload only when a snapshot is known to exist', () => {
    expect(persistenceView(saved).canLoad).toBe(true)
    const fresh = persistenceView({ ...saved, snapshot_exists: false, dirty: true })
    expect(fresh.canLoad).toBe(false)
    expect(fresh.label).toBe('尚未保存')
    expect(fresh.canSave).toBe(true)
  })
  it('blocks writes after a loading error while allowing reload to recover', () => {
    const view = persistenceView({ ...saved, write_blocked: true, error: '文件损坏' })
    expect(view.canSave).toBe(false)
    expect(view.canIngest).toBe(false)
    expect(view.canLoad).toBe(true)
    expect(view.label).toBe('写入已保护')
  })
  it('does not label stale data as saved when a refresh fails', () => {
    const view = persistenceView(null, '网络异常')
    expect(view.label).toBe('状态未知')
    expect(view.canSave).toBe(false)
    expect(view.canLoad).toBe(false)
    expect(view.canIngest).toBe(false)
  })
  it('allows a save retry after a write failure', () => {
    const view = persistenceView({ ...saved, dirty: true, error: '磁盘写入失败' })
    expect(view.label).toBe('持久化异常')
    expect(view.canSave).toBe(true)
  })
  it('shows saved only after a successful confirmed snapshot', () => {
    expect(persistenceView(saved).label).toBe('已保存到本地')
    expect(persistenceView({ ...saved, dirty: true }).label).toBe('有未保存内容')
  })
})
