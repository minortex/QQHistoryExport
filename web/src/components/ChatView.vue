<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import type { ExportTaskView, LibraryConversation, Message, Sender } from '../api'
import MessageItem from './MessageItem.vue'
import { dayKey, formatDate, toDateInput, toTimestamp } from '../utils'

const props = defineProps<{
  conv: LibraryConversation
  selfUin: string
  canRefresh: boolean
  reloadToken: number
  exportDir: string
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
const exportFormat = ref<'html' | 'pdf'>('html')
const exportDirInput = ref('')
const exportCount = ref(0)
const exportTask = ref<ExportTaskView | null>(null)
const exportError = ref('')
const exportPicking = ref(false)
let exportTimer: number | undefined

const senders = ref<Record<string, Sender>>({})
const senderOptions = ref<Sender[]>([])
const messages = ref<Message[]>([])
const loading = ref(false)
const loadingOlder = ref(false)
const hasMore = ref(false)
const covered = ref(false)
const noMore = ref(false)
const olderPending = ref(false)
const pendingAction = ref<'reload' | 'older' | 'day' | null>(null)
const pendingDay = ref('')
const pendingTs = ref(0)
const rangeAll = ref(true)
const dateFrom = ref('')
const dateTo = ref('')
const senderFilter = ref('')
const scroller = ref<HTMLElement | null>(null)
let ignoreScroll = false
let requestSeq = 0

const pillVisible = ref(false)
const pillDate = ref('')
const pillTs = ref(0)
const viewBottomTs = ref(0)
const calendarOpen = ref(false)
const calendarMonth = ref(new Date())
const dayCounts = ref<Record<string, number>>({})
const daysLoading = ref(false)
const daysCacheKey = ref('')
const calStart = ref('')
const calEnd = ref('')
const calHover = ref('')
let pillTimer: number | undefined

const railDays = computed(() => {
  const keys = Object.keys(dayCounts.value).sort()
  const from = toDateInput(bounds().from || props.conv.min_ts || 0)
  const to = toDateInput(bounds().to || props.conv.max_ts || 0)
  const days: { date: string; label: string; count: number }[] = []
  for (const date of keys) {
    if (date < from || date > to) continue
    const [, month, day] = date.split('-').map(Number)
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

const railActiveDay = computed(() => pillDate.value)
const railActiveYear = computed(() => pillDate.value.slice(0, 4))
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
    const resp = await api.messages(props.conv.key, {
      ts_from: bounds().from,
      ts_to: dayEnd - 1,
      sender: senderFilter.value,
      limit: PAGE,
    })
    if (seq !== requestSeq) return
    messages.value = resp.messages
    hasMore.value = true
    covered.value = false
    await nextTick()
    const el = scroller.value
    const index = focusTs ? messages.value.findIndex((msg) => msg.ts >= focusTs) : 0
    const node = el?.querySelectorAll<HTMLElement>('[data-ts]')[Math.max(0, index)]
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
  pillVisible.value = true
  hidePillSoon()
  const index = messages.value.findIndex((msg) => msg.ts >= ts)
  if (index >= 0 && toDateInput(messages.value[index].ts) === date) {
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

function bounds(): { from: number; to: number } {
  if (rangeAll.value) return { from: 0, to: nowTs() }
  return {
    from: toTimestamp(dateFrom.value) || 0,
    to: toTimestamp(dateTo.value, true) || nowTs(),
  }
}

function exportBounds(): { from: number; to: number } {
  if (rangeAll.value) {
    return { from: props.conv.min_ts || 0, to: props.conv.max_ts || nowTs() }
  }
  return bounds()
}

function rangeParams(): Record<string, string | number> {
  const b = bounds()
  return { ts_from: b.from, ts_to: b.to, sender: senderFilter.value }
}

function syncDisplayRange() {
  if (!rangeAll.value) return
  const now = nowTs()
  dateTo.value = toDateInput(props.conv.max_ts || now)
  dateFrom.value = toDateInput(props.conv.min_ts || now - 3 * DAY)
}

const rangeLabel = computed(() => {
  if (rangeAll.value) return '全部时间'
  const fmt = (value: string) => value.replaceAll('-', '/')
  return `${fmt(dateFrom.value)} – ${fmt(dateTo.value)}`
})

function pickPreset(days: number) {
  calendarOpen.value = false
  quickRange(days)
}

function pickAllRange() {
  calendarOpen.value = false
  loadAll()
}

function clearFilters() {
  calendarOpen.value = false
  senderFilter.value = ''
  rangeAll.value = true
  noMore.value = false
  syncDisplayRange()
  void loadLatest()
  daysCacheKey.value = ''
  void loadDays()
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
  inRange: boolean
  rangeStart: boolean
  rangeEnd: boolean
}

const calendarCells = computed<CalendarCell[]>(() => {
  const year = calendarMonth.value.getFullYear()
  const month = calendarMonth.value.getMonth()
  const firstOffset = (new Date(year, month, 1).getDay() + 6) % 7
  const daysInMonth = new Date(year, month + 1, 0).getDate()
  const today = toDateInput(nowTs())
  const start = calStart.value
  const previewEnd = calEnd.value || (start ? calHover.value : '')
  const lo = start && previewEnd ? (start < previewEnd ? start : previewEnd) : start
  const hi =
    start && previewEnd ? (start < previewEnd ? previewEnd : start) : ''
  const cells: CalendarCell[] = []
  const blank = (key: string): CalendarCell => ({
    key,
    date: '',
    label: '',
    count: 0,
    inMonth: false,
    today: false,
    blank: true,
    inRange: false,
    rangeStart: false,
    rangeEnd: false,
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
      inRange: Boolean(lo && hi && date >= lo && date <= hi),
      rangeStart: Boolean(lo && date === lo),
      rangeEnd: Boolean(hi && date === hi),
    })
  }
  while (cells.length % 7 !== 0) cells.push(blank(`t${cells.length}`))
  return cells
})

async function loadSenders() {
  const data = await api.senders(props.conv.key)
  senderOptions.value = data.senders
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
    const resp = await api.messages(props.conv.key, { ...rangeParams(), limit: PAGE })
    if (seq !== requestSeq) return
    messages.value = resp.messages
    hasMore.value = resp.hasMore
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
    syncDisplayRange()
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
  const b = bounds()
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
      ...rangeParams(),
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
  const b = bounds()
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

async function afterOlderImport() {
  pendingAction.value = null
  olderPending.value = false
  syncDisplayRange()
  if (!messages.value.length) {
    await loadLatest()
    return
  }
  const before = messages.value.length
  await fetchOlderPage()
  if (messages.value.length === before) noMore.value = true
  else await prefetchOlder()
}

function applyRange() {
  if (!dateFrom.value || !dateTo.value) return
  if (toTimestamp(dateFrom.value) > toTimestamp(dateTo.value, true)) {
    ;[dateFrom.value, dateTo.value] = [dateTo.value, dateFrom.value]
  }
  rangeAll.value = false
  noMore.value = false
  void loadLatest()
  daysCacheKey.value = ''
  void loadDays()
}

function quickRange(days: number) {
  const anchor = pillDate.value
    ? toTimestamp(pillDate.value, true)
    : messages.value[messages.value.length - 1]?.ts || props.conv.max_ts || nowTs()
  rangeAll.value = false
  dateTo.value = toDateInput(anchor)
  dateFrom.value = toDateInput(anchor - days * DAY)
  noMore.value = false
  void loadLatest()
}

function loadAll() {
  rangeAll.value = true
  noMore.value = false
  syncDisplayRange()
  void loadLatest()
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

async function toggleExport() {
  exportOpen.value = !exportOpen.value
  if (!exportOpen.value) return
  exportError.value = ''
  if (!exportDirInput.value) exportDirInput.value = props.exportDir
  try {
    exportCount.value = (await api.stats(props.conv.key, rangeParams())).count
  } catch {
    exportCount.value = 0
  }
}

async function chooseExportDir() {
  exportPicking.value = true
  exportError.value = ''
  try {
    const result = await api.pickDir()
    if (result.path) exportDirInput.value = result.path
  } catch (err) {
    exportError.value = errorText(err)
  } finally {
    exportPicking.value = false
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

async function startExport() {
  exportError.value = ''
  if (!exportDirInput.value.trim()) {
    exportError.value = '请先选择导出目录'
    return
  }
  const b = exportBounds()
  try {
    const task = await api.exportRange({
      key: props.conv.key,
      format: exportFormat.value,
      dir: exportDirInput.value.trim(),
      tsFrom: b.from,
      tsTo: b.to,
      sender: senderFilter.value,
    })
    exportTask.value = task
    pollExport(task.id)
  } catch (err) {
    exportError.value = errorText(err)
  }
}

function refreshRange() {
  const b = bounds()
  const from = rangeAll.value ? Math.max(b.to - 3 * DAY + 1, 0) : b.from
  pendingAction.value = 'reload'
  olderPending.value = true
  emit('refresh', { from, to: b.to })
}

function currentVisibleDate(): string {
  const el = scroller.value
  if (!el) return ''
  const box = el.getBoundingClientRect()
  const top = box.top + 8
  const bottom = box.bottom - 8
  let first = ''
  let last = 0
  for (const node of el.querySelectorAll<HTMLElement>('[data-ts]')) {
    const rect = node.getBoundingClientRect()
    const ts = Number(node.dataset.ts)
    if (!first && rect.bottom > top) {
      first = toDateInput(ts)
      pillTs.value = ts
    }
    if (rect.top < bottom) last = ts
    else break
  }
  if (last) viewBottomTs.value = last
  return first
}

function hidePillSoon() {
  clearTimeout(pillTimer)
  pillTimer = window.setTimeout(() => {
    if (!calendarOpen.value) pillVisible.value = false
  }, 1400)
}

function syncPill() {
  const next = currentVisibleDate()
  if (next) pillDate.value = next
}

function onScroll() {
  syncPill()
  if (pillDate.value) {
    pillVisible.value = true
    hidePillSoon()
  }
  const el = scroller.value
  if (!el || ignoreScroll) return
  if (el.scrollTop < el.clientHeight * PREFETCH_SCREENS) void prefetchOlder()
}

let daysPromise: Promise<void> | null = null

async function loadDays(): Promise<void> {
  const key = `${props.conv.key}|${senderFilter.value}`
  if (daysCacheKey.value === key) return
  if (daysPromise) return daysPromise
  daysLoading.value = true
  daysPromise = (async () => {
    try {
      const data = await api.days(props.conv.key, senderFilter.value)
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
  calStart.value = rangeAll.value ? '' : dateFrom.value
  calEnd.value = rangeAll.value ? '' : dateTo.value
  calHover.value = ''
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
  calStart.value = ''
  calEnd.value = ''
  calHover.value = ''
  hidePillSoon()
}

function setCalStart(event: Event) {
  calStart.value = (event.target as HTMLInputElement).value
  if (calStart.value && calEnd.value && calEnd.value < calStart.value) calEnd.value = calStart.value
  if (calStart.value) {
    const [y, m] = calStart.value.split('-').map(Number)
    calendarMonth.value = new Date(y, m - 1, 1)
  }
}

function setCalEnd(event: Event) {
  calEnd.value = (event.target as HTMLInputElement).value
  if (calStart.value && calEnd.value && calEnd.value < calStart.value) {
    calStart.value = calEnd.value
  }
}

function setCalHover(date: string) {
  if (!calStart.value || calEnd.value) return
  calHover.value = date
}

function shiftMonth(delta: number) {
  calendarMonth.value = new Date(
    calendarMonth.value.getFullYear(),
    calendarMonth.value.getMonth() + delta,
    1,
  )
}

function pickDay(date: string) {
  if (!calStart.value || calEnd.value) {
    calStart.value = date
    calEnd.value = ''
    return
  }
  if (date < calStart.value) {
    calEnd.value = calStart.value
    calStart.value = date
  } else {
    calEnd.value = date
  }
  applyCalendarRange()
}

function applyCalendarRange() {
  if (!calStart.value || !calEnd.value) return
  dateFrom.value = calStart.value
  dateTo.value = calEnd.value
  pillDate.value = calEnd.value
  calendarOpen.value = false
  calStart.value = ''
  calEnd.value = ''
  calHover.value = ''
  applyRange()
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
    else await loadLatest()
    daysCacheKey.value = ''
    void loadDays()
  },
)

watch(
  () => props.conv.key,
  async () => {
    rangeAll.value = true
    senderFilter.value = ''
    dayCounts.value = {}
    daysCacheKey.value = ''
    calStart.value = ''
    calEnd.value = ''
    syncDisplayRange()
    await loadSenders()
    await loadLatest()
    void loadDays()
  },
  { immediate: true },
)

onMounted(() => window.addEventListener('keydown', onKeydown))
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
          {{ rangeLabel }} <span class="caret">▾</span>
        </button>
        <select v-model="senderFilter" @change="applyRange">
          <option value="">全部成员</option>
          <option v-for="sender in senderOptions" :key="sender.uin" :value="sender.uin">
            {{ sender.name || sender.uin }} ({{ sender.msg_count }})
          </option>
        </select>
        <button v-if="canRefresh" class="chip-btn" @click="refreshRange">刷新</button>
        <button class="chip-btn" @click="toggleExport">导出</button>

        <div v-if="exportOpen" class="range-backdrop" @click="exportOpen = false"></div>
        <div v-if="exportOpen" class="range-popover export-popover" @click.stop>
          <div class="export-row">
            <label for="export-format">格式</label>
            <select id="export-format" v-model="exportFormat">
              <option value="html">HTML（单文件 + 媒体目录）</option>
              <option value="pdf">PDF（reportlab 排版）</option>
            </select>
          </div>
          <div class="export-row">
            <label for="export-dir">目录</label>
            <input
              id="export-dir"
              v-model="exportDirInput"
              type="text"
              spellcheck="false"
              placeholder="~/QQHistoryExport"
            />
            <button class="chip-btn" :disabled="exportPicking" @click="chooseExportDir">
              {{ exportPicking ? '…' : '选择' }}
            </button>
          </div>
          <p class="muted range-tip">
            将导出当前筛选：{{ rangeLabel }} ·
            {{ senderFilter ? '所选成员' : '全部成员' }} · {{ exportCount.toLocaleString() }} 条
          </p>
          <p v-if="exportError" class="export-error">{{ exportError }}</p>
          <div v-if="exportTask" class="export-progress">
            <div class="export-bar"><i :style="{ width: exportTask.progress + '%' }"></i></div>
            <span class="muted">
              {{ exportTask.status === 'running' ? exportTask.progress + '%' : exportTask.message }}
            </span>
          </div>
          <p v-if="exportTask?.status === 'completed'" class="export-done">
            已保存：{{ exportTask.target }}
          </p>
          <p v-if="exportTask?.status === 'failed'" class="export-error">{{ exportTask.error }}</p>
          <div class="range-actions">
            <button class="link-btn" @click="exportOpen = false">关闭</button>
            <span class="range-tip"></span>
            <button
              class="primary"
              :disabled="!exportCount || exportTask?.status === 'running'"
              @click="startExport"
            >
              导出
            </button>
          </div>
        </div>

        <div v-if="calendarOpen" class="range-backdrop" @click="closeCalendar"></div>
        <div v-if="calendarOpen" class="range-popover" @click.stop>
          <div class="range-inputs">
            <label>
              <span>开始</span>
              <input type="date" :value="calStart" @input="setCalStart" />
            </label>
            <span class="range-dash">–</span>
            <label>
              <span>结束</span>
              <input type="date" :value="calEnd" @input="setCalEnd" />
            </label>
          </div>

          <div class="range-presets">
            <button class="chip-btn" @click="pickPreset(7)">7 天</button>
            <button class="chip-btn" @click="pickPreset(30)">30 天</button>
            <button class="chip-btn" @click="pickAllRange">全部</button>
          </div>

          <div
            class="range-cal"
            @mouseleave="calHover = ''"
          >
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
                  :class="{
                    has: cell.count > 0,
                    today: cell.today,
                    'in-range': cell.inRange,
                    'range-start': cell.rangeStart,
                    'range-end': cell.rangeEnd,
                  }"
                  :title="cell.count ? `${cell.date} · ${cell.count} 条` : cell.date"
                  @mouseenter="setCalHover(cell.date)"
                  @click="pickDay(cell.date)"
                >
                  {{ cell.label }}
                  <i v-if="cell.count > 0" class="dot"></i>
                </button>
              </span>
            </div>
          </div>

          <div class="range-actions">
            <button class="link-btn" @click="clearFilters">清空筛选</button>
            <span v-if="daysLoading" class="muted range-tip">正在统计日期…</span>
            <span v-else-if="!calStart" class="muted range-tip">选开始与结束日期</span>
            <button class="primary" :disabled="!calStart || !calEnd" @click="applyCalendarRange">
              应用
            </button>
          </div>
        </div>
      </div>
    </header>

    <div class="messages-wrap">
      <button
        v-if="pillDate"
        class="date-pill"
        :class="{ visible: pillVisible || calendarOpen }"
        :title="'选择日期区间'"
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
              class="day-sep"
            >
              {{ formatDate(msg.ts) }}
            </div>
            <MessageItem
              :msg="msg"
              :senders="senders"
              :self-uin="selfUin"
              :conv-key="conv.key"
              :data-ts="msg.ts"
            />
          </template>
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
            <button
              v-for="day in group.days"
              :key="day.date"
              class="rail-item"
              :class="{ active: day.date === railActiveDay }"
              :data-day="day.date"
              :title="`${day.date.replaceAll('-', '/')} · ${day.count.toLocaleString()} 条`"
              @click="railJumpDay(day.date)"
            >
              <i></i><span>{{ day.label }}</span>
            </button>
          </template>
        </div>
      </nav>
    </div>
  </section>
</template>
