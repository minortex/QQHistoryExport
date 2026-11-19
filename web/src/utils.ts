export function safeKey(convKey: string): string {
  return convKey.replace(':', '_')
}

function encodePath(path: string): string {
  return path
    .split('/')
    .filter((part) => part.length > 0)
    .map((part) => encodeURIComponent(part))
    .join('/')
}

export function mediaUrl(convKey: string, path: string | undefined | null): string {
  if (!path) return ''
  const normalized = path.startsWith('output/') ? path.slice('output/'.length) : path
  if (normalized.startsWith('lazy:')) {
    const md5 = normalized.slice('lazy:'.length)
    return `/lazy/${encodeURIComponent(safeKey(convKey))}/${encodeURIComponent(md5)}`
  }
  return `/media/${encodeURIComponent(safeKey(convKey))}/${encodePath(normalized)}`
}

export function formatTime(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

export function formatDate(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
}

export function dayKey(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
}

export function toDateInput(ts: number): string {
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

export function toTimestamp(dateInput: string, endOfDay = false): number {
  if (!dateInput) return 0
  const [y, m, d] = dateInput.split('-').map(Number)
  const date = endOfDay
    ? new Date(y, m - 1, d, 23, 59, 59)
    : new Date(y, m - 1, d, 0, 0, 0)
  return Math.floor(date.getTime() / 1000)
}

export function formatBytes(size: number | undefined | null): string {
  if (!size) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let value = size
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}
