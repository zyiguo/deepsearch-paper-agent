import { afterEach, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { api } from '../../shared/api'
import SkillOptions from './SkillOptions'

afterEach(() => vi.unstubAllGlobals())

it('reads the skill catalog including warnings', async () => {
  const body = { skills: [{ name: 'review', description: 'Review methods' }], warnings: [], max_active: 3, automatic: true }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body))))
  expect(await api.skills()).toEqual(body)
})

it.each([undefined, [], ['review']])('sends the requested loading mode %j and retains used skills', async (ids) => {
  const turn = { id: 1, question: 'q', created_at: 'now', answer: { text: 'answer', sources: [], skills_used: [{name: 'review', sha256: 'hash'}] } }
  const fetch = vi.fn().mockResolvedValue(new Response(`event: done\ndata: ${JSON.stringify(turn)}\n\n`, {headers: {'Content-Type': 'text/event-stream'}}))
  vi.stubGlobal('fetch', fetch)
  expect(await api.streamTurn('session', 'q', 3, () => {}, undefined, [], [], ids)).toEqual(turn)
  expect(JSON.parse(fetch.mock.calls[0][1].body).skill_ids).toEqual(ids)
})

it('renders accessible loading controls without changing the surrounding form', () => {
  const html = renderToStaticMarkup(createElement(SkillOptions, {value: undefined, onChange: () => {}, disabled: true}))
  expect(html).toContain('技能加载方式')
  expect(html).toContain('自动按需加载')
  expect(html).toContain('指定技能')
  expect(html).toContain('disabled')
})
