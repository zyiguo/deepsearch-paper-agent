export function navigateWorkspace(id: string) {
  if (!/^[a-f0-9]{32}$/.test(id)) throw new Error('无效工作空间')
  const url = new URL(window.location.href)
  url.searchParams.set('workspace', id)
  window.location.assign(url.href)
}
