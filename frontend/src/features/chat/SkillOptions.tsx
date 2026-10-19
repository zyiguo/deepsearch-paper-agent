import { useEffect, useState } from 'react'
import { api } from '../../shared/api'

export default function SkillOptions({ value, onChange, disabled }: {
  value: string[] | undefined; onChange: (value: string[] | undefined) => void; disabled: boolean;
}) {
  const [catalog, setCatalog] = useState<Awaited<ReturnType<typeof api.skills>> | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [manual, setManual] = useState(false)
  async function refresh() {
    setLoading(true); setError('')
    try { setCatalog(await api.skills()) }
    catch { setError('技能目录暂不可用') }
    finally { setLoading(false) }
  }
  useEffect(() => { void refresh() }, [])
  const mode = manual ? 'manual' : value === undefined ? 'auto' : 'off'
  return <fieldset className="skill-options" disabled={disabled}>
    <legend>研究技能</legend>
    <div className="skill-options-toolbar">
      <select aria-label="技能加载方式" value={mode} onChange={event => {
        const next = event.target.value
        setManual(next === 'manual')
        onChange(next === 'auto' ? undefined : [])
      }}>
        <option value="auto" disabled={catalog?.automatic === false}>自动按需加载</option>
        <option value="off">关闭</option>
        <option value="manual">指定技能</option>
      </select>
      <button type="button" onClick={() => void refresh()} disabled={loading} title="刷新技能目录" aria-label="刷新技能目录">↻</button>
    </div>
    {manual && catalog?.skills.map(skill => <label className="skill-choice" key={skill.name} title={skill.description}>
      <input type="checkbox" checked={value?.includes(skill.name) ?? false}
        disabled={!value?.includes(skill.name) && (value?.length ?? 0) >= catalog.max_active}
        onChange={event => onChange(event.target.checked ? [...(value ?? []), skill.name] : value?.filter(name => name !== skill.name))} />
      <span>{skill.name}</span>
    </label>)}
    {error && <p role="status">{error}</p>}
    {catalog?.warnings.map((warning, index) => <p role="status" key={index}>{warning}</p>)}
    {manual && catalog?.skills.length === 0 && <p>暂无可用技能</p>}
    {value?.filter(name => !catalog?.skills.some(skill => skill.name === name)).map(name => <p key={name}>技能已不可用：{name}</p>)}
  </fieldset>
}
