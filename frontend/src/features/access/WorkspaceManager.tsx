import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { accessApi } from './accessApi'
import type { AccessUser, Identity, Member, Workspace } from './accessApi'
import { navigateWorkspace } from './navigation'

export default function WorkspaceManager({ identity, workspace }: { identity: Identity; workspace: Workspace | null }) {
  const [members, setMembers] = useState<Member[]>([])
  const [users, setUsers] = useState<AccessUser[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [confirm, setConfirm] = useState<string | null>(null)
  async function refresh() {
    if (workspace?.role === 'owner') setMembers(await accessApi.members(workspace.id))
    if (identity.user.is_admin) setUsers(await accessApi.users())
  }
  useEffect(() => { void refresh().catch(() => setError('无法读取管理信息，请重试。')) }, [workspace?.id])
  async function run(action: () => Promise<unknown>, refreshAfter = true) {
    if (busy) return
    setBusy(true); setError(''); setNotice('')
    try { await action(); if (refreshAfter) await refresh(); setNotice('已保存。'); setConfirm(null) }
    catch (failure) { setError(failure instanceof Error ? failure.message : '操作失败') }
    finally { setBusy(false) }
  }
  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    void run(async () => { const next = await accessApi.createWorkspace(String(data.get('name'))); navigateWorkspace(next.id) })
  }
  function provision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    void run(async () => { await accessApi.createUser(String(data.get('username')), String(data.get('display_name')), String(data.get('password'))); form.reset() })
  }
  function invite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!workspace) return
    const form = event.currentTarget
    const data = new FormData(form)
    void run(async () => { await accessApi.setMember(workspace.id, String(data.get('username')), data.get('role') as 'editor' | 'viewer'); form.reset() })
  }
  function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    if (data.get('new_password') !== data.get('confirm_password')) { setError('两次新密码不一致。'); return }
    void run(async () => { await accessApi.changePassword(String(data.get('current_password')), String(data.get('new_password'))); form.reset(); window.location.reload() }, false)
  }
  return <main className="access-management"><div className="access-management-title"><span className="access-wordmark">WORKSPACE / SETTINGS</span><h1>空间与账号</h1><p>每个空间拥有独立资料库、会话与研究记录。</p></div>
    <div aria-live="polite">{error && <p className="error" role="alert">{error}</p>}{notice && <p className="notice">{notice}</p>}</div>
    <section className="access-section"><h2>新建工作空间</h2><form onSubmit={create}><label>空间名称<input name="name" required maxLength={100} disabled={busy} /></label><button disabled={busy}>创建空间</button></form></section>
    <section className="access-section"><h2>修改我的密码</h2><p>保存后所有设备的登录都会退出，请使用新密码重新登录。</p><form onSubmit={changePassword}><label>当前密码<input name="current_password" type="password" autoComplete="current-password" required maxLength={128} disabled={busy} /></label><label>新密码<input name="new_password" type="password" autoComplete="new-password" required minLength={12} maxLength={128} disabled={busy} /></label><label>确认新密码<input name="confirm_password" type="password" autoComplete="new-password" required minLength={12} maxLength={128} disabled={busy} /></label><button disabled={busy}>更新密码并退出登录</button></form></section>
    {workspace?.role === 'owner' && <section className="access-section"><h2>{workspace.name} · 成员</h2><p>添加已有账号。可编辑成员能共同使用全部资料；只读成员仅查看已有内容。</p>
      <form onSubmit={invite}><label>用户名<input name="username" required maxLength={64} disabled={busy} /></label><label>权限<select name="role" disabled={busy}><option value="viewer">只读</option><option value="editor">可编辑</option></select></label><button disabled={busy}>添加或更新成员</button></form>
      <ul className="access-members">{members.map(member => <li key={member.id}><span><strong>{member.display_name}</strong><small>{member.username} · {member.role === 'owner' ? '所有者' : member.role === 'editor' ? '可编辑' : '只读'}{member.disabled ? ' · 已停用' : ''}</small></span>{member.role !== 'owner' && <button disabled={busy} onClick={() => void run(() => accessApi.removeMember(workspace.id, member.id))}>移除成员</button>}</li>)}</ul>
    </section>}
    {identity.user.is_admin && <section className="access-section"><h2>系统账号</h2><p>管理员管理账号；访问具体空间仍需要成员权限。</p><form onSubmit={provision} autoComplete="off"><label>用户名<input name="username" required minLength={3} maxLength={64} disabled={busy} /></label><label>显示名称<input name="display_name" required maxLength={100} disabled={busy} /></label><label>初始密码<input name="password" type="password" autoComplete="new-password" required minLength={12} maxLength={128} disabled={busy} /></label><button disabled={busy}>创建账号</button></form>
      <ul className="access-members">{users.map(user => <li key={user.id}><span><strong>{user.display_name}</strong><small>{user.username}{user.is_admin ? ' · 系统管理员' : user.disabled ? ' · 已停用' : ''}</small></span>{!user.is_admin && user.disabled && <button disabled={busy} onClick={() => void run(() => accessApi.enableUser(user.id))}>恢复账号</button>}{!user.is_admin && !user.disabled && (confirm === user.id ? <span><button disabled={busy} onClick={() => void run(() => accessApi.disableUser(user.id))}>确认停用并退出其登录</button><button onClick={() => setConfirm(null)}>取消</button></span> : <button disabled={busy} onClick={() => setConfirm(user.id)}>停用</button>)}</li>)}</ul>
    </section>}
  </main>
}
