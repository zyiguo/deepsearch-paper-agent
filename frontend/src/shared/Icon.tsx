type IconName = 'book' | 'plus' | 'refresh' | 'arrow' | 'file' | 'search' | 'check' | 'copy' | 'external'
const paths: Record<IconName, string> = {
  book: 'M12 7C8 4 5 4 2 5v14c3-1 6-1 10 2m0-14c4-3 7-3 10-2v14c-3-1-6-1-10 2V7',
  plus: 'M12 5v14M5 12h14', refresh: 'M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 14-2l1 6M4 12l1 6a8 8 0 0 0 14-2',
  arrow: 'M4 12h16m-6-6 6 6-6 6', file: 'M14 2H5v20h14V7l-5-5v5h5M8 12h8M8 16h6',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  check: 'm5 12 4 4L19 6', copy: 'M9 9h12v12H9zM15 5V3H3v12h2',
  external: 'M14 3h7v7M21 3l-9 9M10 3H3v18h18v-7',
}
export default function Icon({ name, className = '' }: { name: IconName; className?: string }) {
  return <svg className={`icon ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>
}
