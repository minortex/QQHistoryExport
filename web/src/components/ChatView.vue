<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import type { ExportTaskView, LibraryConversation, Message, Sender } from '../api'
import MessageItem from './MessageItem.vue'
import RailDayItem from './RailDayItem.vue'
import { useScrollSpy } from '@vizejs/ui/scroll-spy'
import { dayKey, formatDate, toDateInput, toTimestamp } from '../utils'

const props = defineProps<{
  conv: LibraryConversation
  selfUin: string
  canRefresh: boolean
  reloadToken: number
}>()
const emit = defineEmits<{
  refresh: [{ from: number; to: number }]
  'load-range': [{ from: number; to: number }]
  'load-older': [{ from: number; to: number }]
}>()

const PAGE = 100
const DAY = 86400
const OLDER_CHUNK = 7 * DAY
const PREFETCH_SCREENS = 3
// 懒加载按“条数”定窗口：一页 100 条，预取 3 屏约 40 条，取两页 200 条避免稀疏群频繁触发
const LOAD_TARGET = 200
const MAX_CHUNK_DAYS = 60

const exportOpen = ref(false)
const exportFormat = ref<'html' | 'pdf' | 'md'>('html')
const exportCount = ref(0)
const exportTask = ref<ExportTaskView | null>(null)
const exportError = ref('')
const exportDir = ref('')
const exportCancelling = ref(false)
const dirOpen = ref(false)
const dirPath = ref('')
const dirParent = ref('')
const dirItems = ref<{ name: string; path: string }[]>([])
const dirLoading = ref(false)
const dirError = ref('')
let exportTimer: number | undefined

const senders = ref<Record<string, Sender>>({})
const messages = ref<Message[]>([])
const loading = ref(false)
const loadingOlder = ref(false)
const loadingNewer = ref(false)
const newerPending = ref(false)
const newerCursorTs = ref(0)
const hasMore = ref(false)
const hasNewer = ref(false)
const covered = ref(false)
const noMore = ref(false)
const olderPending = ref(false)
const pendingAction = ref<'reload' | 'older' | 'day' | 'newer' | null>(null)
const pendingDay = ref('')
const pendingTs = ref(0)
const scroller = ref<HTMLElement | null>(null)
let ignoreScroll = false
let requestSeq = 0

// 选择模式：与当前显示窗口解耦，勾选结果按消息 ID 保存
const selectionMode = ref(false)
const selectedIds = ref<Set<number>>(new Set())
const anchor = ref<{ id: number; ts: number } | null>(null)
const selectingRange = ref(false)
const selectError = ref('')
const selectedCount = computed(() => selectedIds.value.size)
const anchorLabel = computed(() =>
  anchor.value ? `${formatDate(anchor.value.ts)} ${new Date(anchor.value.ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}` : '',
)
watch(selectedCount, (count) => {
  if (selectionMode.value) exportCount.value = count
})

const pillVisible = ref(false)
const pillDate = ref('')
const pillTs = ref(0)
const calendarOpen = ref(false)
const calendarMonth = ref(new Date())
const dayCounts = ref<Record<string, number>>({})
const daysLoading = ref(false)
const daysCacheKey = ref('')
let pillTimer: number | undefined

const railDays = computed(() => {
  const keys = Object.keys(dayCounts.value).sort()
  const from = toDateInput(props.conv.min_ts || 0)
  const to = toDateInput(props.conv.max_ts || 0)
  const days: { date: string; label: string; count: number }[] = []
  for (const date of keys) {
    if (date < from || date > to) continue
    const [, month, day] = date.split("-").map(Number)
    days.push({ date, label: `${month}/${day}`, count: dayCounts.value[date] || 0 })
  }
  return days
})

const railGroups = computed(() => {
  const groups: { year: string; days: { date: string; label: string; count: number }[] }[] = []
  for (const day of railDays.value) {
    const year = day.date.slice(0, 4)
    let group = groups[groups.length - 1]
    if (!group || group.year !== year) {
      group = { year, days: [] }
      groups.push(group)
    }
    group.days.push(day)
  }
  return groups
})

const spyActiveDay = ref('')
const spyIds = computed(() => [...new Set(messages.value.map((msg) => `day-${toDateInput(msg.ts)}`))])
const scrollSpy = useScrollSpy({
  root: scroller,
  ids: spyIds,
  offset: 48,
  onActiveChange: (id) => {
    spyActiveDay.value = id ? id.slice(4) : ''
    if (spyActiveDay.value) {
      pillDate.value = spyActiveDay.value
      pillTs.value = toTimestamp(spyActiveDay.value) || pillTs.value
    }
  },
})
watch(messages, () => nextTick(() => scrollSpy.refresh()), { flush: 'post' })

const railActiveDay = computed(() => spyActiveDay.value || pillDate.value)
const railActiveYear = computed(() => railActiveDay.value.slice(0, 4))
const railListRef = ref<HTMLElement | null>(null)

function centerRail(date: string) {
  const list = railListRef.value
  if (!list || !date) return
  const item = list.querySelector<HTMLElement>(`[data-day="${date}"]`)
  if (!item) return
  const target = item.offsetTop - list.clientHeight / 2 + item.offsetHeight / 2
  list.scrollTop = Math.max(0, Math.min(list.scrollHeight - list.clientHeight, target))
}

watch(railActiveDay, (date) => {
  if (date) nextTick(() => centerRail(date))
})

watch(railDays, () => {
  if (railActiveDay.value) nextTick(() => centerRail(railActiveDay.value))
})

function railJumpDay(date: string) {
  if (!date) return
  void jumpToTs(toTimestamp(date))
}

function railJump(date: string, ts?: number) {
  if (!date) return
  void jumpToTs(ts ?? toTimestamp(date))
}

function railJumpYear(year: string) {
  const group = railGroups.value.find((item) => item.year === year)
  const list = railListRef.value
  const header = list?.querySelector<HTMLElement>(`[data-year="${year}"]`)
  if (list && header) list.scrollTop = Math.max(0, header.offsetTop)
  if (group?.days[0]) railJumpDay(group.days[0].date)
}

function nearestDayWithMessages(date: string): string {
  const keys = Object.keys(dayCounts.value).sort()
  if (!keys.length) return ''
  let lo = 0
  let hi = keys.length - 1
  let next = keys.length
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (keys[mid] < date) lo = mid + 1
    else {
      next = mid
      hi = mid - 1
    }
  }
  const after = keys[next]
  const before = keys[next - 1]
  const distance = (day: string) => Math.abs(toTimestamp(day) - toTimestamp(date))
  let best = ''
  if (after && before) best = distance(after) <= distance(before) ? after : before
  else best = after || before || ''
  return best && distance(best) <= 31 * DAY ? best : ''
}

async function fetchDay(date: string, allowImport: boolean, focusTs = 0) {
  const dayStart = toTimestamp(date)
  const dayEnd = dayStart + DAY
  const seq = ++requestSeq
  loading.value = true
  olderPending.value = false
  noMore.value = false
  try {
    const params: Record<string, string | number> = {
      ts_from: 0,
      ts_to: dayEnd - 1,
      limit: PAGE,
    }
    if (focusTs) params.after_ts = focusTs - 1
    let resp = await api.messages(props.conv.key, params)
    if (focusTs && !resp.messages.length) {
      delete params.after_ts
      resp = await api.messages(props.conv.key, params)
    }
    if (seq !== requestSeq) return
    messages.value = resp.messages
    hasMore.value = true
    hasNewer.value = Boolean(resp.hasNewer)
    loadingNewer.value = false
    newerPending.value = false
    newerCursorTs.value = 0
    covered.value = resp.covered
    await nextTick()
    const el = scroller.value
    const index = focusTs ? messages.value.findIndex((msg) => msg.ts >= focusTs) : 0
    const targetIndex =
      focusTs && index < 0 ? Math.max(0, messages.value.length - 1) : Math.max(0, index)
    const node = el?.querySelectorAll<HTMLElement>('[data-ts]')[targetIndex]
    if (node) {
      node.scrollIntoView({ block: 'start' })
    } else if (el) {
      el.scrollTop = 0
    }
    if (!resp.messages.length && !resp.covered && allowImport) {
      pendingDay.value = date
      pendingTs.value = focusTs
      pendingAction.value = 'day'
      olderPending.value = true
      emit('load-range', { from: dayStart, to: dayEnd - 1 })
      return
    }
    if (!resp.messages.length && allowImport) {
      olderPending.value = true
      await loadDays()
      const snapped = nearestDayWithMessages(date)
      olderPending.value = false
      if (snapped && snapped !== date) {
        pillDate.value = snapped
        await fetchDay(snapped, false)
      }
    }
  } catch (err) {
    console.error(err)
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

async function jumpToTs(ts: number) {
  const date = toDateInput(ts)
  pillDate.value = date
  pillTs.value = ts
  pillVisible.value = true
  hidePillSoon()
  const index = messages.value.findIndex((msg) => msg.ts >= ts)
  if (
    index >= 0 &&
    messages.value[0] &&
    messages.value[0].ts <= ts &&
    toDateInput(messages.value[index].ts) === date
  ) {
    const node = scroller.value?.querySelectorAll<HTMLElement>('[data-ts]')[index]
    if (node) {
      node.scrollIntoView({ block: 'start' })
      return
    }
  }
  await fetchDay(date, true, ts)
}

async function afterDayImport() {
  pendingAction.value = null
  olderPending.value = false
  const date = pendingDay.value
  const focusTs = pendingTs.value
  pendingDay.value = ''
  pendingTs.value = 0
  if (date) await fetchDay(date, false, focusTs)
  else await loadLatest()
}

function nowTs(): number {
  return Math.floor(Date.now() / 1000)
}

function displayBounds(): { from: number; to: number } {
  return { from: 0, to: nowTs() }
}

async function toggleSelectMode() {
  const el = scroller.value
  const beforeTop = el?.getBoundingClientRect().top ?? 0
  const beforeScroll = el?.scrollTop ?? 0
  selectionMode.value = !selectionMode.value
  clearSelection()
  exportOpen.value = false
  await nextTick()
  if (el) {
    const delta = el.getBoundingClientRect().top - beforeTop
    el.scrollTop = Math.max(0, beforeScroll + delta)
  }
}

function clearSelection() {
  selectedIds.value = new Set()
  anchor.value = null
  selectError.value = ''
}

async function addRangeIds(from: number, to: number) {
  if (selectingRange.value) return
  selectingRange.value = true
  selectError.value = ''
  try {
    const data = await api.messageIds(props.conv.key, {
      ts_from: Math.floor(from),
      ts_to: Math.ceil(to),
    })
    const next = new Set(selectedIds.value)
    for (const id of data.ids) next.add(id)
    selectedIds.value = next
  } catch (err) {
    selectError.value = errorText(err)
  } finally {
    selectingRange.value = false
  }
}

async function toggleSelected(msg: Message, shift = false) {
  if (shift && anchor.value) {
    const from = Math.min(anchor.value.ts, msg.ts)
    const to = Math.max(anchor.value.ts, msg.ts) + 1
    await addRangeIds(from, to)
    return
  }
  const next = new Set(selectedIds.value)
  if (next.has(msg.id)) {
    next.delete(msg.id)
    if (anchor.value?.id === msg.id) {
      const fallback = messages.value.find((item) => next.has(item.id))
      anchor.value = fallback ? { id: fallback.id, ts: fallback.ts } : null
    }
  } else {
    next.add(msg.id)
    if (!anchor.value) anchor.value = { id: msg.id, ts: msg.ts }
  }
  selectedIds.value = next
}

function firstVisibleTs(): number {
  const el = scroller.value
  if (!el) return 0
  const top = el.getBoundingClientRect().top
  for (const node of el.querySelectorAll<HTMLElement>('[data-ts]')) {
    if (node.getBoundingClientRect().bottom > top + 2) {
      return Number(node.dataset.ts) || 0
    }
  }
  return 0
}

function lastVisibleTs(): number {
  const el = scroller.value
  if (!el) return 0
  const bottom = el.getBoundingClientRect().bottom
  let last = 0
  for (const node of el.querySelectorAll<HTMLElement>('[data-ts]')) {
    if (node.getBoundingClientRect().top < bottom - 2) {
      last = Number(node.dataset.ts) || last
    } else {
      break
    }
  }
  return last
}

async function selectToCurrent() {
  if (!anchor.value) return
  const first = firstVisibleTs()
  const last = lastVisibleTs()
  if (!first || !last) return
  // 视点在起点之前 → 覆盖到视口第一条；视点在起点之后 → 覆盖到视口最后一条
  let target: number
  if (anchor.value.ts > last) target = first
  else if (anchor.value.ts < first) target = last
  else target = last
  await addRangeIds(
    Math.min(anchor.value.ts, target),
    Math.max(anchor.value.ts, target) + 1,
  )
}

function selectLoadedMessages() {
  const next = new Set(selectedIds.value)
  for (const msg of messages.value) next.add(msg.id)
  selectedIds.value = next
}

const pillLabel = computed(() => {
  if (!pillDate.value) return ''
  const [y, m, d] = pillDate.value.split('-').map(Number)
  return new Date(y, m - 1, d).toLocaleDateString('zh-CN', {
    month: 'long',
    day: 'numeric',
    weekday: 'short',
  })
})

const monthLabel = computed(
  () => `${calendarMonth.value.getFullYear()} 年 ${calendarMonth.value.getMonth() + 1} 月`,
)

interface CalendarCell {
  key: string
  date: string
  label: string
  count: number
  inMonth: boolean
  today: boolean
  blank: boolean
}

const calendarCells = computed<CalendarCell[]>(() => {
  const year = calendarMonth.value.getFullYear()
  const month = calendarMonth.value.getMonth()
  const firstOffset = (new Date(year, month, 1).getDay() + 6) % 7
  const daysInMonth = new Date(year, month + 1, 0).getDate()
  const today = toDateInput(nowTs())
  const cells: CalendarCell[] = []
  const blank = (key: string): CalendarCell => ({
    key,
    date: '',
    label: '',
    count: 0,
    inMonth: false,
    today: false,
    blank: true,
  })
  for (let i = 0; i < firstOffset; i++) cells.push(blank(`b${i}`))
  for (let day = 1; day <= daysInMonth; day++) {
    const date = toDateInput(new Date(year, month, day).getTime() / 1000)
    cells.push({
      key: date,
      date,
      label: String(day),
      count: dayCounts.value[date] || 0,
      inMonth: true,
      today: date === today,
      blank: false,
    })
  }
  while (cells.length % 7 !== 0) cells.push(blank(`t${cells.length}`))
  return cells
})

async function loadSenders() {
  const data = await api.senders(props.conv.key)
  const map: Record<string, Sender> = {}
  for (const sender of data.senders) map[sender.uin] = sender
  senders.value = map
}

async function prepend(list: Message[]) {
  const el = scroller.value
  const height = el?.scrollHeight || 0
  const top = el?.scrollTop || 0
  messages.value = [...list, ...messages.value]
  await nextTick()
  if (el) {
    ignoreScroll = true
    el.scrollTop = top + (el.scrollHeight - height)
    setTimeout(() => {
      ignoreScroll = false
    }, 0)
  }
}

async function loadLatest() {
  const seq = ++requestSeq
  loading.value = true
  noMore.value = false
  olderPending.value = false
  pendingAction.value = null
  try {
    const resp = await api.messages(props.conv.key, { limit: PAGE })
    if (seq !== requestSeq) return
    messages.value = resp.messages
    hasMore.value = resp.hasMore
    hasNewer.value = false
    loadingNewer.value = false
    newerPending.value = false
    newerCursorTs.value = 0
    covered.value = resp.covered
    await nextTick()
    const el = scroller.value
    if (el) {
      ignoreScroll = true
      el.scrollTop = el.scrollHeight
      setTimeout(() => {
        ignoreScroll = false
      }, 0)
    }
    pillVisible.value = false
    if (!messages.value.length) {
      if (!resp.covered) requestRangeImport(latestChunk())
      else noMore.value = true
      return
    }
    syncPill()
  } catch (err) {
    if (seq === requestSeq) messages.value = []
    console.error(err)
  } finally {
    if (seq === requestSeq) loading.value = false
  }
  if (seq === requestSeq) await prefetchOlder()
}

function latestChunk(): { from: number; to: number } {
  const b = displayBounds()
  return { from: Math.max(b.to - 3 * DAY + 1, b.from), to: b.to }
}

function requestRangeImport(range: { from: number; to: number }) {
  olderPending.value = true
  pendingAction.value = 'reload'
  emit('load-range', range)
}

async function fetchOlderPage(): Promise<boolean> {
  if (loadingOlder.value) return false
  const first = messages.value[0]
  if (!first) return false
  loadingOlder.value = true
  try {
    const resp = await api.messages(props.conv.key, {
      limit: PAGE,
      before_ts: first.ts,
      before_id: first.id,
    })
    hasMore.value = resp.hasMore
    covered.value = resp.covered
    if (!resp.messages.length) return false
    await prepend(resp.messages)
    return true
  } catch (err) {
    console.error(err)
    return false
  } finally {
    loadingOlder.value = false
  }
}

let prefetching = false

async function prefetchOlder() {
  if (prefetching || loading.value) return
  prefetching = true
  try {
    for (let i = 0; i < 6; i++) {
      const el = scroller.value
      if (!el || olderPending.value || noMore.value) return
      if (el.scrollTop > el.clientHeight * PREFETCH_SCREENS) return
      if (hasMore.value) {
        if (!(await fetchOlderPage())) return
        continue
      }
      if (covered.value) {
        noMore.value = true
        return
      }
      requestOlderImport()
      return
    }
  } finally {
    prefetching = false
  }
}

function requestNewerImport(from: number, to: number) {
  if (newerPending.value) return
  if (from > to) {
    hasNewer.value = false
    return
  }
  newerPending.value = true
  pendingAction.value = 'newer'
  emit('load-range', { from, to })
}

async function fetchNewerPage(): Promise<boolean> {
  if (loadingNewer.value || newerPending.value || !hasNewer.value) return false
  const last = messages.value[messages.value.length - 1]
  if (!last) return false
  loadingNewer.value = true
  try {
    const maxTs = props.conv.max_ts || nowTs()
    for (let i = 0; i < 6; i++) {
      const from = newerCursorTs.value || last.ts
      const to = Math.min(from + OLDER_CHUNK - 1, maxTs)
      if (from > maxTs) {
        hasNewer.value = false
        return false
      }
      const resp = await api.messages(props.conv.key, {
        limit: PAGE,
        ts_from: from,
        ts_to: to,
        after_ts: last.ts,
        after_id: last.id,
      })
      if (resp.messages.length) {
        const seen = new Set(messages.value.map((msg) => msg.id))
        messages.value = [
          ...messages.value,
          ...resp.messages.filter((msg) => !seen.has(msg.id)),
        ]
        newerCursorTs.value = 0
        hasNewer.value = Boolean(resp.hasNewer || to < maxTs)
        covered.value = resp.covered
        return true
      }
      if (!resp.covered) {
        requestNewerImport(from, to)
        hasNewer.value = true
        covered.value = false
        return false
      }
      if (to >= maxTs) {
        hasNewer.value = false
        covered.value = true
        return false
      }
      newerCursorTs.value = to + 1
    }
    return false
  } catch (err) {
    console.error(err)
    hasNewer.value = false
    return false
  } finally {
    loadingNewer.value = false
  }
}

let newerPrefetching = false

async function prefetchNewer() {
  if (newerPrefetching || loading.value || newerPending.value) return
  newerPrefetching = true
  try {
    for (let i = 0; i < 6; i++) {
      const el = scroller.value
      if (!el || !hasNewer.value) return
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight
      if (distance >= el.clientHeight * PREFETCH_SCREENS) return
      if (!(await fetchNewerPage())) return
      await nextTick()
    }
  } finally {
    newerPrefetching = false
  }
}

function olderChunkStart(to: number, floor: number): number {
  const counts = dayCounts.value
  if (!Object.keys(counts).length) return Math.max(to - OLDER_CHUNK + 1, floor)
  let cursor = toTimestamp(toDateInput(to))
  let total = 0
  let start = cursor
  for (let i = 0; i < MAX_CHUNK_DAYS; i++) {
    total += counts[toDateInput(cursor)] || 0
    start = cursor
    if (total >= LOAD_TARGET) break
    cursor -= DAY
  }
  return Math.max(start, floor)
}

function requestOlderImport() {
  if (olderPending.value || loadingOlder.value || noMore.value) return
  const b = displayBounds()
  const first = messages.value[0]
  const to = first ? first.ts - 1 : b.to
  const from = olderChunkStart(to, b.from)
  if (from > to || to < b.from) {
    noMore.value = true
    return
  }
  olderPending.value = true
  pendingAction.value = 'older'
  emit('load-older', { from, to })
}

async function loadOlder() {
  if (loading.value || loadingOlder.value || olderPending.value || noMore.value) return
  if (hasMore.value) {
    await fetchOlderPage()
  } else {
    requestOlderImport()
  }
}

async function afterNewerImport() {
  pendingAction.value = null
  newerPending.value = false
  loadingNewer.value = false
  if (!messages.value.length) {
    await loadLatest()
    return
  }
  await prefetchNewer()
}

async function afterOlderImport() {
  pendingAction.value = null
  olderPending.value = false
  if (!messages.value.length) {
    await loadLatest()
    return
  }
  const before = messages.value.length
  await fetchOlderPage()
  if (messages.value.length === before) noMore.value = true
  else await prefetchOlder()
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function toggleExport() {
  exportOpen.value = !exportOpen.value
  if (!exportOpen.value) return
  exportError.value = ''
  exportCount.value = selectionMode.value ? selectedCount.value : props.conv.msg_count
  if (exportTask.value?.status !== 'running') exportTask.value = null
  if (!exportDir.value) loadExportDir()
}

async function loadExportDir() {
  try {
    exportDir.value = (await api.exportDir()).path
  } catch {
    exportDir.value = ''
  }
}

async function openDirPicker() {
  dirOpen.value = true
  dirError.value = ''
  await loadDirs(exportDir.value)
}

async function loadDirs(path: string) {
  dirLoading.value = true
  dirError.value = ''
  try {
    const res = await api.listDirs(path)
    dirPath.value = res.path
    dirParent.value = res.parent
    dirItems.value = res.dirs
  } catch (err) {
    dirError.value = errorText(err)
  } finally {
    dirLoading.value = false
  }
}

async function chooseDir() {
  try {
    const res = await api.setExportDir(dirPath.value)
    exportDir.value = res.path
    dirOpen.value = false
  } catch (err) {
    dirError.value = errorText(err)
  }
}

function stopExportPoll() {
  if (exportTimer) window.clearInterval(exportTimer)
  exportTimer = undefined
}

function pollExport(id: string) {
  stopExportPoll()
  exportTimer = window.setInterval(async () => {
    try {
      const task = await api.exportStatus(id)
      exportTask.value = task
      if (task.status !== 'running') stopExportPoll()
    } catch (err) {
      exportError.value = errorText(err)
      stopExportPoll()
    }
  }, 800)
}

async function cancelExport() {
  const task = exportTask.value
  if (!task || task.status !== 'running' || exportCancelling.value) return
  exportCancelling.value = true
  try {
    await api.cancelExport(task.id)
  } catch (err) {
    exportError.value = errorText(err)
  } finally {
    exportCancelling.value = false
  }
}

async function startExport() {
  exportError.value = ''
  if (selectionMode.value && !selectedCount.value) {
    exportError.value = '请先勾选要导出的消息'
    return
  }
  try {
    const task = selectionMode.value
      ? await api.exportRange({
          key: props.conv.key,
          format: exportFormat.value,
          ids: [...selectedIds.value],
        })
      : await api.exportRange({
          key: props.conv.key,
          format: exportFormat.value,
          tsFrom: props.conv.min_ts || 0,
          tsTo: (props.conv.max_ts || nowTs()) + 1,
        })
    exportTask.value = task
    pollExport(task.id)
  } catch (err) {
    exportError.value = errorText(err)
  }
}

function refreshRange() {
  const b = displayBounds()
  const from = Math.max(b.to - 3 * DAY + 1, 0)
  pendingAction.value = 'reload'
  olderPending.value = true
  emit('refresh', { from, to: b.to })
}

function syncPill() {
  if (!spyActiveDay.value) return
  pillDate.value = spyActiveDay.value
  pillTs.value = toTimestamp(spyActiveDay.value) || pillTs.value
}

function hidePillSoon() {
  clearTimeout(pillTimer)
  pillTimer = window.setTimeout(() => {
    if (!calendarOpen.value) pillVisible.value = false
  }, 1400)
}

function onScroll() {
  syncPill()
  if (pillDate.value) {
    pillVisible.value = true
    hidePillSoon()
  }
  const el = scroller.value
  if (!el || ignoreScroll) return
  if (el.scrollTop < el.clientHeight * PREFETCH_SCREENS) {
    void prefetchOlder()
  } else if (
    el.scrollHeight - el.scrollTop - el.clientHeight <
    el.clientHeight * PREFETCH_SCREENS
  ) {
    void prefetchNewer()
  }
}

let daysPromise: Promise<void> | null = null

async function loadDays(): Promise<void> {
  const key = props.conv.key
  if (daysCacheKey.value === key) return
  if (daysPromise) return daysPromise
  daysLoading.value = true
  daysPromise = (async () => {
    try {
      const data = await api.days(props.conv.key)
      const map: Record<string, number> = {}
      for (const item of data.days) map[item.date] = item.count
      dayCounts.value = map
      daysCacheKey.value = key
    } catch (err) {
      console.error(err)
    } finally {
      daysLoading.value = false
      daysPromise = null
    }
  })()
  return daysPromise
}

async function openCalendar() {
  calendarOpen.value = true
  clearTimeout(pillTimer)
  pillVisible.value = true
  syncPill()
  const base = pillDate.value || toDateInput(props.conv.max_ts || nowTs())
  const [y, m] = base.split('-').map(Number)
  calendarMonth.value = new Date(y, m - 1, 1)
  await loadDays()
}

function toggleCalendar() {
  if (calendarOpen.value) closeCalendar()
  else void openCalendar()
}

function closeCalendar() {
  calendarOpen.value = false
  hidePillSoon()
}

function shiftMonth(delta: number) {
  calendarMonth.value = new Date(
    calendarMonth.value.getFullYear(),
    calendarMonth.value.getMonth() + delta,
    1,
  )
}

function pickDay(date: string) {
  calendarOpen.value = false
  pillDate.value = date
  pillVisible.value = false
  void jumpToTs(toTimestamp(date))
}

function jumpLatest() {
  calendarOpen.value = false
  void loadLatest()
}

function jumpEarliest() {
  calendarOpen.value = false
  void jumpToTs(props.conv.min_ts || 0)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && calendarOpen.value) {
    closeCalendar()
    return
  }
  if (!event.altKey) return
  if (event.key === 'ArrowLeft') {
    event.preventDefault()
    void loadOlder()
  }
}

watch(
  () => props.reloadToken,
  async () => {
    const action = pendingAction.value
    pendingAction.value = null
    olderPending.value = false
    if (action === 'older') await afterOlderImport()
    else if (action === 'day') await afterDayImport()
    else if (action === 'newer') await afterNewerImport()
    else await loadLatest()
    daysCacheKey.value = ''
    void loadDays()
  },
)

watch(
  () => props.conv.key,
  async () => {
    dayCounts.value = {}
    daysCacheKey.value = ''
    clearSelection()
    hasNewer.value = false
    newerPending.value = false
    newerCursorTs.value = 0
    await loadSenders()
    await loadLatest()
    void loadDays()
  },
  { immediate: true },
)

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  loadExportDir()
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  stopExportPoll()
})

</script>

<template>
  <section class="chat">
    <header class="chat-header">
      <div class="title-row">
        <h2>{{ conv.name }}</h2>
        <span class="chip">{{ conv.kind === 'troop' ? '群聊' : '好友' }}</span>
        <span class="chip muted-chip">{{ conv.uin }}</span>
        <span class="muted">已加载 {{ conv.msg_count.toLocaleString() }} 条</span>
      </div>
      <div class="filters">
        <button class="chip-btn range-chip" @click="toggleCalendar">
          {{ pillLabel || '跳转日期' }} <span class="caret">▾</span>
        </button>
        <button v-if="canRefresh" class="chip-btn" @click="refreshRange">刷新</button>
        <button
          class="chip-btn"
          :class="{ 'chip-active': selectionMode }"
          :title="selectionMode ? '退出选择模式' : '进入选择模式'"
          @click="toggleSelectMode"
        >
          选择
        </button>
        <button class="chip-btn" @click="toggleExport">导出</button>

        <div v-if="exportOpen" class="range-backdrop" @click="exportOpen = false"></div>
        <div v-if="exportOpen" class="range-popover export-popover" @click.stop>
          <div class="export-row">
            <label for="export-format">格式</label>
            <select id="export-format" v-model="exportFormat">
              <option value="html">HTML（单文件 + 媒体目录）</option>
              <option value="pdf">PDF（reportlab 排版）</option>
              <option value="md">Markdown（纯文本，无图片）</option>
            </select>
          </div>
          <div class="export-row">
            <label>导出到</label>
            <span class="dir-value" :title="exportDir">{{ exportDir || '默认文件夹' }}</span>
            <button class="link-btn" @click="openDirPicker">选择…</button>
          </div>
          <p class="muted range-tip">
            文件名为「会话名_时间戳」，直接保存到所选文件夹；HTML 含大媒体时打包为 zip。
          </p>
          <p class="muted range-tip">
            <template v-if="selectionMode">将导出已勾选的 {{ exportCount.toLocaleString() }} 条</template>
            <template v-else>将导出整个会话（{{ exportCount.toLocaleString() }} 条）</template>
          </p>
          <p v-if="exportError" class="export-error">{{ exportError }}</p>
          <div v-if="exportTask" class="export-progress">
            <div class="export-bar"><i :style="{ width: exportTask.progress + '%' }"></i></div>
            <span class="muted">
              {{ exportTask.status === 'running' ? exportTask.progress + '%' : exportTask.message }}
            </span>
          </div>
          <p v-if="exportTask?.status === 'completed'" class="export-done">
            已保存：{{ exportTask.output }}
          </p>
          <p v-if="exportTask?.status === 'failed'" class="export-error">{{ exportTask.error }}</p>
          <div class="range-actions">
            <button class="link-btn" @click="exportOpen = false">关闭</button>
            <span class="range-tip"></span>
            <button
              v-if="exportTask?.status === 'running'"
              class="link-btn"
              :disabled="exportCancelling"
              @click="cancelExport"
            >
              {{ exportCancelling ? '正在取消…' : '取消导出' }}
            </button>
            <button
              class="primary"
              :disabled="!exportCount || exportTask?.status === 'running'"
              @click="startExport"
            >
              导出
            </button>
          </div>
        </div>

        <div v-if="dirOpen" class="range-backdrop" @click="dirOpen = false"></div>
        <div v-if="dirOpen" class="range-popover dir-popover" @click.stop>
          <div class="export-row">
            <label>文件夹</label>
            <span class="dir-value" :title="dirPath">{{ dirPath }}</span>
          </div>
          <div class="dir-list">
            <button v-if="dirParent" class="dir-item" @click="loadDirs(dirParent)">..</button>
            <button v-for="item in dirItems" :key="item.path" class="dir-item" @click="loadDirs(item.path)">
              {{ item.name }}
            </button>
            <p v-if="dirLoading" class="muted range-tip">读取中…</p>
            <p v-else-if="!dirItems.length" class="muted range-tip">没有子文件夹</p>
          </div>
          <p v-if="dirError" class="export-error">{{ dirError }}</p>
          <div class="range-actions">
            <button class="link-btn" @click="dirOpen = false">取消</button>
            <span class="range-tip"></span>
            <button class="primary" @click="chooseDir">选择此文件夹</button>
          </div>
        </div>

        <div v-if="calendarOpen" class="range-backdrop" @click="closeCalendar"></div>
        <div v-if="calendarOpen" class="range-popover" @click.stop>
          <div class="range-cal">
            <div class="cal-head">
              <button class="icon-btn" @click="shiftMonth(-1)">‹</button>
              <strong>{{ monthLabel }}</strong>
              <button class="icon-btn" @click="shiftMonth(1)">›</button>
            </div>
            <div class="cal-week">
              <span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span>
            </div>
            <div class="cal-grid">
              <span v-for="cell in calendarCells" :key="cell.key" class="cal-cell">
                <button
                  v-if="!cell.blank"
                  class="cal-day"
                  :class="{ has: cell.count > 0, today: cell.today }"
                  :title="cell.count ? `${cell.date} · ${cell.count} 条` : cell.date"
                  @click="pickDay(cell.date)"
                >
                  {{ cell.label }}
                  <i v-if="cell.count > 0" class="dot"></i>
                </button>
              </span>
            </div>
          </div>

          <div class="range-actions">
            <button class="link-btn" @click="jumpEarliest">最早</button>
            <button class="link-btn" @click="jumpLatest">最新</button>
            <span v-if="daysLoading" class="muted range-tip">正在统计日期…</span>
            <span v-else class="muted range-tip">点击日期跳转</span>
          </div>
        </div>
      </div>
    </header>

    <div v-if="selectionMode" class="selection-bar">
      <strong>已选 {{ selectedCount.toLocaleString() }} 条</strong>
      <button
        class="chip-btn"
        :disabled="!anchor || selectingRange"
        :title="anchorLabel ? `起点 ${anchorLabel}` : '先勾选一条消息作为起点'"
        @click="selectToCurrent"
      >
        选中到当前
      </button>
      <button class="chip-btn" @click="selectLoadedMessages">全选当前屏</button>
      <button class="chip-btn" :disabled="!selectedCount" @click="clearSelection">清空</button>
      <button class="chip-btn primary" :disabled="!selectedCount" @click="toggleExport">
        导出所选
      </button>
    </div>

    <div class="messages-wrap">
      <div v-if="selectError || selectingRange" class="selection-toast">
        {{ selectingRange ? '正在取区间…' : selectError }}
      </div>
      <button
        v-if="pillDate"
        class="date-pill"
        :class="{ visible: pillVisible || calendarOpen }"
        :title="'跳转日期'"
        @click="openCalendar"
      >
        {{ pillLabel }}
      </button>

      <div ref="scroller" class="messages" @scroll.passive="onScroll">
        <div class="msg-list">
          <div v-if="loadingOlder || olderPending" class="load-status">正在加载更早…</div>
          <div v-else-if="noMore && messages.length" class="load-status muted">
            已经到最早的消息
          </div>
          <button
            v-else-if="messages.length && !hasMore && !covered"
            class="load-status load-more"
            @click="loadOlder"
          >
            继续加载更早
          </button>
          <template v-for="(msg, index) in messages" :key="msg.id">
            <div
              v-if="index === 0 || dayKey(msg.ts) !== dayKey(messages[index - 1].ts)"
              :id="`day-${toDateInput(msg.ts)}`"
              class="day-sep"
            >
              {{ formatDate(msg.ts) }}
            </div>
            <MessageItem
              :msg="msg"
              :senders="senders"
              :self-uin="selfUin"
              :conv-key="conv.key"
              :selectable="selectionMode"
              :selected="selectedIds.has(msg.id)"
              @toggle="(payload) => toggleSelected(msg, payload.shift)"
            />
          </template>
          <div v-if="loadingNewer || newerPending" class="load-status">正在加载更新…</div>
          <button
            v-else-if="hasNewer && messages.length"
            class="load-status load-more"
            @click="prefetchNewer"
          >
            继续加载更新
          </button>
          <p v-if="!messages.length && !loading" class="muted empty">
            {{ olderPending ? '正在加载该区间…' : '该区间没有消息' }}
          </p>
        </div>
      </div>

      <nav v-if="railDays.length" class="date-rail" aria-label="日期索引">
        <div class="rail-years">
          <button
            v-for="year in railGroups"
            :key="year.year"
            class="rail-year-btn"
            :class="{ active: year.year === railActiveYear }"
            @click="railJumpYear(year.year)"
          >
            {{ year.year }}
          </button>
        </div>
        <div ref="railListRef" class="rail-days">
          <template v-for="group in railGroups" :key="group.year">
            <div class="rail-year" :data-year="group.year">{{ group.year }}</div>
            <RailDayItem
              v-for="day in group.days"
              :key="day.date"
              :conv-key="conv.key"
              :day="day"
              :active="day.date === railActiveDay"
              @jump="railJump"
            />
          </template>
        </div>
      </nav>
    </div>
  </section>
</template>
