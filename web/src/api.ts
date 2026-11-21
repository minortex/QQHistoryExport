export interface ConfigView {
  dbPath: string
  dbstPath: string
  mediaRoot: string
  imagesPath: string
  voicePath: string
  videoPath: string
  selfQQ: string
  hasKey: boolean
  ready: boolean
  needImages: boolean
  needVoice: boolean
  needVideo: boolean
  needQQEmoji: boolean
  qqEmojiVer: string
  needMarketFace: boolean
  needJavaDeser: boolean
  needAvatars: boolean
  isAdb: boolean
  adbSerial: string
  adbSqlite: string
  dataDir?: string
  cacheLimitMB?: number
}

export interface RawConversation {
  key: string
  kind: 'troop' | 'friend'
  uin: string
  name: string
  count_main: number
  count_slow: number
  total: number
  imported: boolean
  resolved: boolean
}

export interface ConversationRow {
  key: string
  kind: string
  uin: string
  name: string
  total: number
  imported: boolean
  resolved: boolean
}

export interface LibraryConversation {
  key: string
  kind: string
  uin: string
  name: string
  msg_count: number
  min_ts: number
  max_ts: number
  imported_at: number
}

export interface Sender {
  uin: string
  name: string
  avatar: string
  msg_count: number
}

export interface BlockContent {
  m?: string
  text?: string
  path?: string
  thumb?: string
  name?: string
  type?: string
  size?: number
  index?: string
  duration?: number | string
  [key: string]: unknown
}

export interface Block {
  t?: string
  c?: BlockContent
}

export interface MessagePayload {
  t?: string
  s?: string
  i?: number
  c?: Block[] | BlockContent
  [key: string]: unknown
}

export interface Message {
  id: number
  ts: number
  sender: string
  type: string
  payload: MessagePayload
}

export function blockList(payload: MessagePayload): Block[] {
  const content = payload.c
  if (!content) return []
  if (Array.isArray(content)) return content
  // 非 msg 类型（img/voice/file/tip/uns…）的 c 就是单个内容块
  return [{ t: payload.t, c: content }]
}

export interface JobView {
  id: string
  convKey: string
  convName: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  stage: string
  progress: number
  message: string
  error: string
  loaded: number
  foreground: boolean
  suspended: boolean
  created: number
}

export interface ExportTaskView {
  id: string
  convKey: string
  convName: string
  format: 'html' | 'pdf' | 'md'
  output: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  message: string
  error: string
  created: number
}

export interface DayCount {
  date: string
  count: number
}

export interface TimeBucket {
  key: string
  count: number
}

export interface MessagesPage {
  messages: Message[]
  hasMore: boolean
  hasNewer?: boolean
  covered: boolean
  tsFrom: number
  tsTo: number
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      const body = await resp.json()
      detail = body.detail || detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return resp.json() as Promise<T>
}

export const api = {
  state: () =>
    req<{ config: ConfigView; conversations: LibraryConversation[]; jobs: JobView[] }>(
      '/api/state',
    ),
  setup: (payload: Record<string, unknown>) =>
    req<{ config: ConfigView }>('/api/setup', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  discover: () => req<{ conversations: RawConversation[] }>('/api/discover'),
  importConversation: (
    key: string,
    tsFrom: number,
    tsTo: number,
    refresh = false,
    foreground = true,
  ) =>
    req<JobView>('/api/import', {
      method: 'POST',
      body: JSON.stringify({ key, tsFrom, tsTo, refresh, foreground }),
    }),
  foreground: (key: string) =>
    req<{ job: JobView | null }>('/api/foreground', {
      method: 'POST',
      body: JSON.stringify({ key }),
    }),
  job: (id: string) => req<JobView>(`/api/jobs/${id}`),
  cancelJob: (id: string) =>
    req<{ ok: boolean }>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  conversations: () =>
    req<{ conversations: LibraryConversation[] }>('/api/conversations'),
  senders: (key: string) =>
    req<{ senders: Sender[] }>(`/api/conversations/${encodeURIComponent(key)}/senders`),
  days: (key: string) =>
    req<{ days: DayCount[] }>(
      `/api/conversations/${encodeURIComponent(key)}/days`,
    ),
  buckets: (key: string, date: string, unit: 'hour' | 'minute', hour?: string) => {
    const query = new URLSearchParams({ date, unit })
    if (hour !== undefined) query.set('hour', String(Number(hour)))
    return req<{ buckets: TimeBucket[] }>(
      `/api/conversations/${encodeURIComponent(key)}/buckets?${query.toString()}`,
    )
  },
  messages: (key: string, params: Record<string, string | number>) => {
    const query = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v !== '' && v !== undefined && v !== null) query.set(k, String(v))
    }
    return req<MessagesPage>(
      `/api/conversations/${encodeURIComponent(key)}/messages?${query.toString()}`,
    )
  },
  messageIds: (key: string, params: Record<string, string | number>) => {
    const query = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v !== '' && v !== undefined && v !== null) query.set(k, String(v))
    }
    return req<{ ids: number[] }>(
      `/api/conversations/${encodeURIComponent(key)}/ids?${query.toString()}`,
    )
  },
  exportRange: (payload: {
    key: string
    format: 'html' | 'pdf' | 'md'
    tsFrom?: number
    tsTo?: number
    sender?: string
    ids?: number[]
  }) =>
    req<ExportTaskView>('/api/export', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  exportStatus: (id: string) => req<ExportTaskView>(`/api/export/${id}`),
  cancelExport: (id: string) =>
    req<{ ok: boolean }>(`/api/export/${id}/cancel`, { method: 'POST' }),
  exportDir: () => req<{ path: string }>('/api/export-dir'),
  setExportDir: (path: string) =>
    req<{ path: string }>('/api/export-dir', {
      method: 'POST',
      body: JSON.stringify({ path }),
    }),
  listDirs: (path = '') =>
    req<{ path: string; parent: string; dirs: { name: string; path: string }[] }>(
      `/api/fs/dirs?${new URLSearchParams({ path }).toString()}`,
    ),
  stats: (key: string, params: Record<string, string | number>) => {
    const query = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v !== '' && v !== undefined && v !== null) query.set(k, String(v))
    }
    return req<{ count: number }>(
      `/api/conversations/${encodeURIComponent(key)}/stats?${query.toString()}`,
    )
  },
}
