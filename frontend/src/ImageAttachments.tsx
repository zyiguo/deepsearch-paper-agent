import type { ImageAttachment } from './api'

export default function ImageAttachments({ attachments, disabled = false, onRemove, onIndex, onReuse, indexed = [] }: {
  attachments: ImageAttachment[]; disabled?: boolean; indexed?: string[];
  onRemove?: (id: string) => void; onIndex?: (id: string) => void; onReuse?: (image: ImageAttachment) => void;
}) {
  if (!attachments.length) return null
  return <div className="image-attachments" aria-label="图片附件">
    {attachments.map(image => <figure className="image-attachment" key={image.id}>
      <a href={image.image_url} target="_blank" rel="noreferrer" title={`查看 ${image.title}`}>
        <img src={image.image_url} alt={image.title} loading="lazy" width={144} height={96} />
      </a>
      <figcaption title={image.title}>{image.title}</figcaption>
      <div className="attachment-actions">
        {onIndex && <button type="button" disabled={disabled || indexed.includes(image.id)} title="将图片发送至已配置的模型服务并建立索引，可能计费" onClick={() => onIndex(image.id)}>{indexed.includes(image.id) ? '已提交入库' : '加入资料库'}</button>}
        {onReuse && <button type="button" disabled={disabled} onClick={() => onReuse(image)}>用于提问</button>}
        {onRemove && <button type="button" className="attachment-remove" disabled={disabled} aria-label={`移除附件 ${image.title}`} title="从本次提问移除" onClick={() => onRemove(image.id)}>×</button>}
      </div>
    </figure>)}
  </div>
}
