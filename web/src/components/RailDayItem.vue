<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref } from 'vue'
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from '@vizejs/ui/hover-card'
import { api } from '../api'
import type { TimeBucket } from '../api'
import type { HoverCardRootExpose } from '@vizejs/ui/hover-card'
import { toTimestamp } from '../utils'

const props = defineProps<{
  convKey: string
  day: { date: string; label: string; count: number }
  active: boolean
}>()
const emit = defineEmits<{
  jump: [date: string, ts?: number]
}>()

interface DaySelection {
  hour: string
  minute: string
}

const selections = new Map<string, DaySelection>()

const hours = ref<TimeBucket[]>([])
const minutes = ref<TimeBucket[]>([])
const currentHour = ref('')
const activeMinute = ref('')
const loadingHours = ref(false)
const loadingMinutes = ref(false)
const loadedHours = ref(false)
const loadedMinuteHour = ref('')
const hoursError = ref('')
const minutesError = ref('')
const minutesRef = ref<HTMLElement | null>(null)
const hoursRef = ref<HTMLElement | null>(null)
const cardRef = ref<HoverCardRootExpose | null>(null)
let minuteSeq = 0
let hoursPromise: Promise<void> | null = null
let hourHoverTimer: number | undefined

const HOUR_HOVER_DELAY = 140

function selectionKey(): string {
  return `${props.convKey}|${props.day.date}`
}

async function loadHours(): Promise<void> {
  if (loadedHours.value) return
  if (!hoursPromise) {
    hoursPromise = (async () => {
      loadingHours.value = true
      hoursError.value = ''
      try {
        const data = await api.buckets(props.convKey, props.day.date, 'hour')
        hours.value = data.buckets
        loadedHours.value = true
        const saved = selections.get(selectionKey())
        const preferred =
          saved && hours.value.some((hour) => hour.key === saved.hour)
            ? saved.hour
            : hours.value[0]?.key
        if (preferred) await selectHour(preferred)
      } catch (err) {
        hoursError.value = err instanceof Error ? err.message : String(err)
      } finally {
        loadingHours.value = false
      }
    })().finally(() => {
      hoursPromise = null
    })
  }
  return hoursPromise
}

function cancelHourHover() {
  if (hourHoverTimer === undefined) return
  window.clearTimeout(hourHoverTimer)
  hourHoverTimer = undefined
}

function scheduleHourHover(hour: string) {
  cancelHourHover()
  hourHoverTimer = window.setTimeout(() => {
    hourHoverTimer = undefined
    void selectHour(hour)
  }, HOUR_HOVER_DELAY)
}

onBeforeUnmount(cancelHourHover)

async function selectHour(hour: string) {
  if (currentHour.value !== hour) activeMinute.value = ''
  if (
    currentHour.value === hour &&
    (loadingMinutes.value || loadedMinuteHour.value === hour)
  ) {
    return
  }
  currentHour.value = hour
  minutes.value = []
  minutesError.value = ''
  loadedMinuteHour.value = ''
  const seq = ++minuteSeq
  loadingMinutes.value = true
  try {
    const data = await api.buckets(props.convKey, props.day.date, 'minute', hour)
    if (seq === minuteSeq) {
      minutes.value = data.buckets
      loadedMinuteHour.value = hour
    }
  } catch (err) {
    if (seq === minuteSeq) {
      minutesError.value = err instanceof Error ? err.message : String(err)
    }
  } finally {
    if (seq === minuteSeq) loadingMinutes.value = false
  }
}

async function restoreSelection() {
  await loadHours()
  const saved = selections.get(selectionKey())
  if (!saved || !hours.value.some((hour) => hour.key === saved.hour)) return
  if (currentHour.value !== saved.hour) await selectHour(saved.hour)
  activeMinute.value = saved.minute
  await scrollActiveMinute()
  await scrollActiveHour()
}

function onOpenChange(open: boolean) {
  if (open) {
    void restoreSelection()
  } else {
    cancelHourHover()
  }
}

function onHourClick(hour: string) {
  cancelHourHover()
  activeMinute.value = ''
  selections.set(selectionKey(), { hour, minute: '' })
  void selectHour(hour)
}

async function onMinuteClick(minute: string) {
  activeMinute.value = minute
  selections.set(selectionKey(), { hour: currentHour.value, minute })
  emit('jump', props.day.date, minuteTs(currentHour.value, minute))
  cardRef.value?.setOpen(false)
  await scrollActiveMinute()
}

async function scrollActiveMinute() {
  await nextTick()
  const node = minutesRef.value?.querySelector<HTMLElement>('.rail-card-minute.active')
  node?.scrollIntoView({ block: 'nearest' })
}

async function scrollActiveHour() {
  await nextTick()
  const node = hoursRef.value?.querySelector<HTMLElement>('.rail-card-hour.active')
  node?.scrollIntoView({ block: 'nearest' })
}

function hourTs(hour: string): number {
  return toTimestamp(props.day.date) + Number(hour) * 3600
}

function minuteTs(hour: string, minute: string): number {
  return hourTs(hour) + Number(minute) * 60
}
</script>

<template>
  <HoverCard
    ref="cardRef"
    :open-delay="250"
    :close-delay="200"
    touch-behavior="long-press"
    @update:open="onOpenChange"
  >
    <HoverCardTrigger
      as="button"
      type="button"
      class="rail-item"
      :class="{ active }"
      :data-day="day.date"
      :title="`${day.date.replaceAll('-', '/')} · ${day.count.toLocaleString()} 条`"
      @click="emit('jump', day.date)"
    >
      <i></i><span>{{ day.label }}</span>
    </HoverCardTrigger>

    <HoverCardContent
      placement="left-start"
      :collision-padding="12"
      :aria-label="day.date"
    >
      <div class="rail-card">
        <div
          v-if="currentHour"
          ref="minutesRef"
          class="rail-card-minutes"
          @mouseenter="cancelHourHover"
        >
          <div class="rail-card-title">{{ currentHour }} 时</div>
          <p v-if="loadingMinutes" class="rail-card-empty">加载中…</p>
          <p v-else-if="minutesError" class="rail-card-empty">{{ minutesError }}</p>
          <template v-else>
            <button
              v-for="minute in minutes"
              :key="minute.key"
              type="button"
              class="rail-card-minute"
              :class="{ active: minute.key === activeMinute }"
              @click="onMinuteClick(minute.key)"
            >
              <span>{{ currentHour }}:{{ minute.key }}</span><em>{{ minute.count }}</em>
            </button>
            <p v-if="!minutes.length" class="rail-card-empty">该小时无消息</p>
          </template>
        </div>

        <div ref="hoursRef" class="rail-card-hours">
          <div class="rail-card-title">{{ day.date }}</div>
          <p v-if="loadingHours" class="rail-card-empty">加载中…</p>
          <p v-else-if="hoursError" class="rail-card-empty">{{ hoursError }}</p>
          <template v-else>
            <button
              v-for="hour in hours"
              :key="hour.key"
              type="button"
              class="rail-card-hour"
              :class="{ active: hour.key === currentHour }"
              @mouseenter="scheduleHourHover(hour.key)"
              @mouseleave="cancelHourHover"
              @focus="scheduleHourHover(hour.key)"
              @blur="cancelHourHover"
              @click="onHourClick(hour.key)"
            >
              <span>{{ hour.key }}:00</span><em>{{ hour.count }}</em>
            </button>
            <p v-if="!hours.length" class="rail-card-empty">无消息</p>
          </template>
        </div>
      </div>
    </HoverCardContent>
  </HoverCard>
</template>
