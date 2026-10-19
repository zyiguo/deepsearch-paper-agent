export const imageAccept = '.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp'

export function imageContentType(file: File): string {
  const suffix = file.name.toLowerCase().split('.').pop()
  if (suffix === 'png') return 'image/png'
  if (suffix === 'jpg' || suffix === 'jpeg') return 'image/jpeg'
  if (suffix === 'webp') return 'image/webp'
  return file.type
}

export function validateImageFile(file: File): string | null {
  if (!/\.(png|jpe?g|webp)$/i.test(file.name)) return '请选择 PNG、JPEG 或 WebP 图片。'
  if (!file.size || file.size > 10 * 1024 * 1024) return '每张图片须非空且不超过 10 MiB。'
  return null
}
