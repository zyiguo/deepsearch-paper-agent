import { useEffect, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { ApiError } from '../../shared/api'
import { configureAccessScope } from '../../shared/accessScope'
import { accessApi } from './accessApi'
import type { Identity, Workspace } from './accessApi'
import WorkspaceManager from './WorkspaceManager'
import { navigateWorkspace } from './navigation'
import './access.css'

export function LoginPanel({ onSubmit, busy, error }: { onSubmit: (event: FormEvent<HTMLFormElement>) => void; busy: boolean; error: string }) {
  return <section className="access-card" aria-labelledby="login-title"><span className="access-wordmark">D· / DeepSearch</span>
    <h1 id="login-title">进入研究工作台</h1><p>登录后访问你所属的工作空间。</p>
    <form onSubmit={onSubmit}><label>用户名<input name="username" autoComplete="username" required maxLength={64} disabled={busy} autoFocus /></label>
      <label>密码<input name="password" type="password" autoComplete="current-password" required maxLength={128} disabled={busy} /></label>
      {error && <p className="error" role="alert">{error}</p>}<button className="primary-button" disabled={busy}>{busy ? '正在登录…' : '登录'}</button>
    </form><small>没有账号时，请联系系统管理员。</small></section>
}

export default function AccessGate({ children }: { children: (readOnly: boolean) => ReactNode }) {
  const [mode, setMode] = useState<'loading' | 'local' | 'team'>('loading')
  const [identity, setIdentity] = useState<Identity | null>(null)
  const [active, setActive] = useState<Workspace | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [manage, setManage] = useState(false)
  useEffect(() => {
    let disposed = false
    void accessApi.status().then(async ({ mode: value }) => {
      if (disposed) return
      if (value === 'local') { configureAccessScope(null); setMode('local'); return }
      try {
        const me = await accessApi.me()
        if (disposed) return
        const requested = new URL(window.location.href).searchParams.get('workspace')
        const chosen = requested ? me.workspaces.find(w => w.id === requested) ?? null : me.workspaces[0] ?? null
        if (chosen) configureAccessScope(chosen.id, me.user.id)
        setIdentity(me); setActive(chosen)
        if (requested && !chosen) setError('该工作空间不存在或已无权访问，请选择其他空间。')
      } catch (failure) {
        if (!disposed && !(failure instanceof ApiError && failure.status === 401)) setError('无法读取账号信息，请刷新重试。')
      }
      if (!disposed) setMode('team')
    }).catch(() => { if (!disposed) setError('无法连接账号服务。请检查后端后刷新。') })
    const expired = () => window.location.reload()
    window.addEventListener('deepsearch:unauthorized', expired)
    return () => { disposed = true; window.removeEventListener('deepsearch:unauthorized', expired) }
  }, [])

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    setBusy(true); setError('')
    try { await accessApi.login(String(data.get('username')), String(data.get('password'))); form.reset(); window.location.reload() }
    catch (failure) { setError(failure instanceof Error ? failure.message : '登录失败') }
    finally { setBusy(false) }
  }
  async function logout() {
    setBusy(true); setError('')
    try { await accessApi.logout(); window.location.reload() }
    catch { setError('退出未确认，请重试。') }
    finally { setBusy(false) }
  }
  if (mode === 'loading') return <div className="access-screen"><section className="access-card"><p role="status">{error || '正在连接工作台…'}</p>{error && <button onClick={() => window.location.reload()}>重新连接</button>}</section></div>
  if (mode === 'local') return children(false)
  if (!identity) return <div className="access-screen"><LoginPanel onSubmit={login} busy={busy} error={error} /></div>
  return <div className="team-shell"><header className="access-toolbar"><span><strong>{identity.user.display_name}</strong><small>{active?.role === 'viewer' ? '只读成员' : active?.role === 'owner' ? '空间所有者' : '研究成员'}</small></span>
    <label>工作空间<select aria-label="当前工作空间" value={active?.id ?? ''} onChange={event => navigateWorkspace(event.target.value)}><option value="" disabled>请选择</option>{identity.workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}</select></label>
    <button onClick={() => setManage(value => !value)}>{manage ? '返回研究' : '空间与账号'}</button><button disabled={busy} onClick={() => void logout()}>退出登录</button></header>
    {error && <p className="error" role="alert">{error}</p>}
    {(manage || !active) ? <WorkspaceManager identity={identity} workspace={active} /> : <>{active.role === 'viewer' && <p className="access-readonly">你可以查看文献与已有研究结果；发起研究或修改资料需要可编辑权限。</p>}{children(active.role === 'viewer')}</>}
  </div>
}
