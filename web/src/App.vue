<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from './api'
import type {
  ConfigView,
  ConversationRow,
  JobView,
  LibraryConversation,
  RawConversation,
} from './api'
import ChatView from './components/ChatView.vue'
import ConversationList from './components/ConversationList.vue'
import SetupPanel from './components/SetupPanel.vue'
import { toDateInput, toTimestamp } from './utils'

const config = ref<ConfigView | null>(null)
const library = ref<LibraryConversation[]>([])
const discovered = ref<RawConversation[]>([])
const currentKey = ref('')
const jobs = ref<Record<string, JobView>>({})
const starting = ref<Record<string, boolean>>({})
const showSetup = ref(false)
const error = ref('')
const scanning = ref(false)
const sidebarOpen = ref(true)
const reloadTokens = ref<Record<string, number>>({})
const pendingFocus = ref<string | null>(null)
const olderState = new Map<string, { from: number; to: number; chain: number }>()
const sources = new Map<string, EventSource>()

const DAY = 86400
const OLDER_STEP = 7 * DAY

function isRunningJob(job: JobView | null | undefined): boolean {
  return job?.status === 'running' || job?.status === 'pending'
}

function convStarting(key: string): boolean {
  return Boolean(starting.value[key])
}

function convImporting(key: string): boolean {
  if (!key) return false
  if (convStarting(key)) return true
  return Object.values(jobs.value).some((job) => job.convKey === key && isRunningJob(job))
}

function bumpReload(key: string) {
  reloadTokens.value[key] = (reloadTokens.value[key] || 0) + 1
}

function recentRange(): { from: number; to: number } {
  const now = Math.floor(Date.now() / 1000)
  return {
    from: toTimestamp(toDateInput(now - 2 * DAY)),
    to: toTimestamp(toDateInput(now), true),
  }
}

const current = computed(() => library.value.find((c) => c.key === currentKey.value) || null)
const currentJob = computed(() => {
  const list = Object.values(jobs.value).filter((job) => job.convKey === currentKey.value)
  return list.find(isRunningJob) || list[0] || null
})
const currentStarting = computed(() => convStarting(currentKey.value))
const currentBusy = computed(() => currentStarting.value || isRunningJob(currentJob.value))
const backgroundRunning = computed(
  () =>
    Object.values(jobs.value).filter(
      (job) => job.convKey !== currentKey.value && isRunningJob(job),
    ).length,
)
const needsSetup = computed(() => !config.value?.ready || showSetup.value)

const rows = computed<ConversationRow[]>(() => {
  const result: ConversationRow[] = discovered.value.map((item) => ({
    key: item.key,
    kind: item.kind,
    uin: item.uin,
    name: item.name,
    total: item.total,
    imported: item.imported,
    resolved: item.resolved,
  }))
  const seen = new Set(result.map((row) => row.key))
  for (const conv of library.value) {
    if (seen.has(conv.key)) continue
    result.push({
      key: conv.key,
      kind: conv.kind,
      uin: conv.uin,
      name: conv.name,
      total: conv.msg_count,
      imported: true,
      resolved: true,
    })
  }
  return result
})

async function refreshLibrary() {
  const data = await api.conversations()
  library.value = data.conversations
  if (!currentKey.value && library.value.length) currentKey.value = library.value[0].key
}

async function scan() {
  if (!config.value?.ready) {
    discovered.value = []
    return
  }
  scanning.value = true
  error.value = ''
  try {
    const data = await api.discover()
    discovered.value = data.conversations
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    scanning.value = false
  }
}

async function bootstrap() {
  const state = await api.state()
  config.value = state.config
  library.value = state.conversations
  if (library.value.length) currentKey.value = library.value[0].key
  for (const job of state.jobs) {
    jobs.value[job.id] = job
    listenJob(job.id)
  }
}

async function onSaved(next: ConfigView) {
  config.value = next
  showSetup.value = false
  discovered.value = []
  await refreshLibrary()
}

function select(key: string) {
  const row = rows.value.find((item) => item.key === key)
  if (!row) return
  pendingFocus.value = null
  if (row.imported || library.value.some((conv) => conv.key === key)) {
    currentKey.value = key
    if (window.matchMedia('(max-width: 900px)').matches) sidebarOpen.value = false
    return
  }
  if (!row.resolved) {
    error.value = "该会话在 Friends/TroopInfoV2 中找不到对应 QQ 号，无法加载"
    return
  }
  if (convImporting(key)) {
    pendingFocus.value = key
    return
  }
  pendingFocus.value = key
  const range = recentRange()
  void importConv(key, range.from, range.to)
}

function refreshCurrent(range: { from: number; to: number }) {
  const key = currentKey.value
  if (!config.value?.isAdb || !key || convImporting(key)) return
  void importConv(key, range.from, range.to, true)
}

function loadRange(range: { from: number; to: number }) {
  const key = currentKey.value
  if (!key || convImporting(key)) return
  olderState.delete(key)
  void importConv(key, range.from, range.to)
}

function loadOlder(range: { from: number; to: number }) {
  const key = currentKey.value
  if (!key || convImporting(key)) return
  olderState.set(key, { ...range, chain: 0 })
  void importConv(key, range.from, range.to, false, true)
}

function listenJob(id: string) {
  sources.get(id)?.close()
  const source = new EventSource(`/api/jobs/${id}/events`)
  sources.set(id, source)
  source.onmessage = (event) => {
    const data = JSON.parse(event.data) as {
      stage?: string
      progress?: number
      message?: string
      status?: string
      error?: string
      loaded?: number
    }
    if (data.stage === 'status') {
      source.close()
      sources.delete(id)
      const job = jobs.value[id]
      if (job) {
        job.status = (data.status as JobView['status']) || 'failed'
        job.error = data.error || ''
        if (typeof data.loaded === 'number') job.loaded = data.loaded
      }
      void finishJob(id)
      return
    }
    const job = jobs.value[id]
    if (!job) return
    if (data.stage) job.stage = data.stage
    if (typeof data.progress === 'number') job.progress = data.progress
    if (data.message) job.message = data.message
    if (data.stage === 'failed' && data.error) job.error = data.error
  }
  source.onerror = () => {
    source.close()
    sources.delete(id)
  }
}

async function finishJob(id: string) {
  const finished = jobs.value[id]
  if (!finished) return
  setTimeout(
    () => {
      if (jobs.value[id] === finished) delete jobs.value[id]
    },
    finished.status === 'failed' ? 8000 : 2600,
  )
  await refreshLibrary()
  const item = discovered.value.find((conv) => conv.key === finished.convKey)
  if (item) item.imported = true
  const pending = olderState.get(finished.convKey)
  if (
    finished.status === 'completed' &&
    pending &&
    finished.loaded === 0 &&
    pending.chain < 4
  ) {
    const to = pending.from - 1
    const from = to - OLDER_STEP + 1
    olderState.set(finished.convKey, { from, to, chain: pending.chain + 1 })
    await importConv(finished.convKey, from, to, false, true)
    return
  }
  olderState.delete(finished.convKey)
  if (finished.status === 'completed') {
    bumpReload(finished.convKey)
    if (pendingFocus.value === finished.convKey) {
      currentKey.value = finished.convKey
      pendingFocus.value = null
    }
  } else {
    bumpReload(finished.convKey)
    if (pendingFocus.value === finished.convKey) pendingFocus.value = null
  }
}

async function importConv(
  key: string,
  tsFrom: number,
  tsTo: number,
  refresh = false,
  keepOlder = false,
) {
  error.value = ''
  if (!keepOlder) olderState.delete(key)
  starting.value[key] = true
  try {
    const started = await api.importConversation(key, tsFrom, tsTo, refresh)
    for (const [id, existing] of Object.entries(jobs.value)) {
      if (existing.convKey === key && !isRunningJob(existing)) delete jobs.value[id]
    }
    jobs.value[started.id] = started
    listenJob(started.id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    if (pendingFocus.value === key) pendingFocus.value = null
  } finally {
    delete starting.value[key]
  }
}

async function cancelJob() {
  const job = currentJob.value
  if (!job || !isRunningJob(job)) return
  await api.cancelJob(job.id)
}

function dismissJob() {
  const job = currentJob.value
  if (job && !isRunningJob(job)) delete jobs.value[job.id]
}

async function markForeground(key: string) {
  try {
    const { job } = await api.foreground(key)
    if (!job) return
    const existing = jobs.value[job.id]
    if (existing) {
      existing.suspended = job.suspended
      existing.foreground = job.foreground
    } else {
      jobs.value[job.id] = job
      listenJob(job.id)
    }
  } catch {
    /* 抢占失败不阻塞浏览 */
  }
}

watch(currentKey, (key) => {
  if (key) void markForeground(key)
})

onMounted(() => {
  void bootstrap()
})
</script>

<template>
  <div class="app">
    <div v-if="currentBusy" class="top-progress">
      <div class="bar" :style="{ width: (currentJob?.progress || 0) + '%' }" />
    </div>
    <header class="topbar">
      <div class="brand">
        <button
          class="icon-btn"
          :title="sidebarOpen ? '收起会话列表' : '展开会话列表'"
          @click="sidebarOpen = !sidebarOpen"
        >
          ☰
        </button>
        <strong>QQ 聊天记录</strong>
        <span v-if="config?.ready" class="muted brand-account">
          {{ config.selfQQ || '未知账号' }}
        </span>
      </div>
      <div class="actions">
        <div
          v-if="currentStarting || currentJob || backgroundRunning"
          class="job-chip"
          :class="currentJob?.status"
          :title="currentJob?.error || currentJob?.message"
        >
          <span
            v-if="currentStarting || isRunningJob(currentJob) || (!currentJob && backgroundRunning)"
            class="spinner"
          />
          <span class="job-text">
            <template v-if="currentStarting">准备中…</template>
            <template v-else-if="currentJob && isRunningJob(currentJob)">
              {{ currentJob.stage }} {{ currentJob.progress }}%
            </template>
            <template v-else-if="currentJob?.status === 'completed'">
              已加载 {{ currentJob.loaded.toLocaleString() }} 条
            </template>
            <template v-else-if="currentJob?.status === 'cancelled'">已取消</template>
            <template v-else-if="currentJob">加载失败</template>
            <template v-else>后台加载中 {{ backgroundRunning }}</template>
          </span>
          <button
            v-if="currentJob"
            class="icon-btn tiny"
            :title="isRunningJob(currentJob) ? '取消' : '关闭'"
            @click="isRunningJob(currentJob) ? cancelJob() : dismissJob()"
          >
            ×
          </button>
        </div>
        <button class="ghost small" @click="showSetup = true">设置</button>
      </div>
    </header>

    <div v-if="error" class="toast error">{{ error }}</div>

    <main class="layout" :class="{ 'sidebar-hidden': !sidebarOpen }">
      <ConversationList
        :rows="rows"
        :current-key="currentKey"
        :scanning="scanning"
        @select="select"
        @scan="scan"
      />
      <ChatView
        v-if="current"
        :key="current.key"
        :conv="current"
        :self-uin="config?.selfQQ || ''"
        :can-refresh="Boolean(config?.isAdb) && !convImporting(current.key)"
        :reload-token="reloadTokens[current.key] || 0"
        :export-dir="config?.exportDir || ''"
        @refresh="refreshCurrent"
        @load-range="loadRange"
        @load-older="loadOlder"
      />
      <section v-else class="empty-state">
        <h2>还没有选择会话</h2>
        <p class="muted">
          在左侧选择一个会话，首次打开会自动加载；之后按日期、成员浏览聊天记录。
        </p>
      </section>
    </main>

    <SetupPanel
      v-if="needsSetup && config"
      :config="config"
      :closable="config.ready"
      @saved="onSaved"
      @close="showSetup = false"
    />
  </div>
</template>
