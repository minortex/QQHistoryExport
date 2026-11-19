<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { blockList } from '../api'
import type { Block, Message, Sender } from '../api'
import { formatBytes, formatTime, mediaUrl } from '../utils'

const props = defineProps<{
  msg: Message
  senders: Record<string, Sender>
  selfUin: string
  convKey: string
  selectable?: boolean
  selected?: boolean
}>()
const emit = defineEmits<{
  toggle: [{ shift: boolean }]
}>()

function onToggle(event: MouseEvent) {
  emit('toggle', { shift: event.shiftKey })
}

const payload = computed(() => props.msg.payload)
const blocks = computed<Block[]>(() => blockList(props.msg.payload))
const sender = computed(() => props.senders[props.msg.sender])
const isSelf = computed(() => props.msg.sender === props.selfUin)
const displayName = computed(
  () => sender.value?.name || props.msg.sender,
)
const avatarFailed = ref(false)
const failedImages = ref<Set<number>>(new Set())

function onImageError(index: number) {
  const next = new Set(failedImages.value)
  next.add(index)
  failedImages.value = next
}
watch(
  () => props.msg.sender,
  () => {
    avatarFailed.value = false
  },
)
const avatar = computed(() => {
  if (avatarFailed.value) return ''
  const stored = sender.value?.avatar
  if (stored) return mediaUrl(props.convKey, stored)
  const uin = sender.value?.uin || props.msg.sender
  return uin ? `/avatars/${encodeURIComponent(uin)}.jpg` : ''
})
function onAvatarError() {
  avatarFailed.value = true
}
const isSystem = computed(
  () =>
    ['tip', 'revoke', 'nudge'].includes(payload.value.t || '') ||
    (payload.value.t === 'uns' && contentOf(blocks.value[0])?.type === 'tip'),
)

function contentOf(block: Block | undefined) {
  return block?.c
}

function textOf(block: Block): string {
  return String(block.c?.m ?? block.c?.text ?? '')
}

function imageUrl(block: Block): string {
  return mediaUrl(props.convKey, block.c?.path)
}

function imageStyle(block: Block): Record<string, string> | undefined {
  if (block.t !== 'img') return undefined
  const width = Number(block.c?.w)
  const height = Number(block.c?.h)
  if (!width || !height) return undefined
  // 预留与最终渲染一致的尺寸，懒加载完成后列表高度不再变化
  const scale = Math.min(1, 320 / Math.max(width, height))
  return {
    width: `${Math.round(width * scale)}px`,
    height: `${Math.round(height * scale)}px`,
  }
}

function openImage(url: string) {
  window.open(url, '_blank', 'noopener')
}
</script>

<template>
  <div class="msg-row" :class="{ selectable, selected }">
    <label class="msg-check" :class="{ invisible: !selectable }" @click.stop>
      <input
        type="checkbox"
        :checked="selected"
        :tabindex="selectable ? 0 : -1"
        @click="onToggle"
      />
    </label>
    <div class="msg-content">
      <div v-if="isSystem" class="system-line">
        {{ blocks.map(textOf).join(' ') || payload.t }}
      </div>
      <div v-else class="message" :class="{ self: isSelf }">
    <img v-if="avatar" class="avatar" :src="avatar" :alt="displayName" @error="onAvatarError" />
    <div v-else class="avatar avatar-empty">{{ displayName.slice(0, 1) }}</div>
    <div class="body">
      <div class="meta">
        <span class="name">{{ displayName }}</span>
        <span class="time">{{ formatTime(msg.ts) }}</span>
      </div>
      <div class="bubble">
        <template v-for="(block, index) in blocks" :key="index">
          <span v-if="block.t === 'm'" class="text">{{ textOf(block) }}</span>

          <div v-else-if="block.t === 'reply'" class="reply">
            <span class="reply-label">引用</span>{{ block.c?.text }}
          </div>

          <template
            v-else-if="block.t === 'img' || block.t === 'marketface' || block.t === 'qqemoji'"
          >
            <img
              v-if="!failedImages.has(index)"
              class="image"
              :class="{ emoji: block.t === 'qqemoji' }"
              :style="imageStyle(block)"
              :src="imageUrl(block)"
              :alt="block.c?.name || '图片'"
              loading="lazy"
              @error="onImageError(index)"
              @click="openImage(imageUrl(block))"
            />
            <span v-else class="unknown">[图片不可用]</span>
          </template>

          <audio
            v-else-if="block.t === 'voice'"
            class="audio"
            :src="imageUrl(block)"
            controls
            preload="none"
          />

          <video
            v-else-if="block.t === 'video'"
            class="video"
            :src="imageUrl(block)"
            :poster="mediaUrl(props.convKey, block.c?.thumb)"
            controls
            preload="metadata"
          />

          <a
            v-else-if="block.t === 'file'"
            class="file"
            :href="imageUrl(block)"
            target="_blank"
            rel="noopener"
          >
            📎 {{ block.c?.name || block.c?.path }}
            <em v-if="block.c?.size">{{ formatBytes(block.c.size) }}</em>
          </a>

          <span v-else-if="block.t === 'uns' || block.t === 'err'" class="unknown">
            {{ textOf(block) }}
          </span>

          <span v-else class="unknown">[{{ block.t || '未支持' }}]</span>
        </template>
      </div>
    </div>
      </div>
    </div>
  </div>
</template>
