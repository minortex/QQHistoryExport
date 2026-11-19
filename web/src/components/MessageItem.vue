<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { blockList } from '../api'
import type { Block, Message, Sender } from '../api'
import { formatBytes, formatTime, mediaUrl } from '../utils'

// 与 QQ 一致：聊天气泡内图片的长边上限 135dp、短边下限 45dp
const IMAGE_MAX = 135
const IMAGE_MIN = 45

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

function isMarketFace(block: Block): boolean {
  return block.t === 'img' && block.c?.type === 'marketface'
}

function imageStyle(block: Block): Record<string, string> | undefined {
  if (block.t !== 'img' || isMarketFace(block)) return undefined
  const width = Number(block.c?.w)
  const height = Number(block.c?.h)
  if (!width || !height) return undefined
  const short = Math.min(width, height)
  const long = Math.max(width, height)
  let scale = 1
  if (short < IMAGE_MIN) {
    scale = IMAGE_MIN / short
  } else if (long > IMAGE_MAX) {
    scale = Math.max(IMAGE_MAX / long, IMAGE_MIN / short)
  }
  return {
    width: `${Math.round(width * scale)}px`,
    height: `${Math.round(height * scale)}px`,
  }
}

function zoomable(block: Block): boolean {
  return block.t === 'img' && !isMarketFace(block)
}

const viewerSrc = ref('')

function openImage(url: string) {
  viewerSrc.value = url
}

function closeViewer() {
  viewerSrc.value = ''
}

function onViewerKey(event: KeyboardEvent) {
  if (event.key === 'Escape') closeViewer()
}

watch(viewerSrc, (value) => {
  if (value) window.addEventListener('keydown', onViewerKey)
  else window.removeEventListener('keydown', onViewerKey)
})

onBeforeUnmount(() => window.removeEventListener('keydown', onViewerKey))
</script>

<template>
  <div class="msg-row" :class="{ selectable, selected }" :data-ts="msg.ts">
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
              :class="{ emoji: block.t === 'qqemoji', marketface: isMarketFace(block), sized: !!imageStyle(block) }"
              :style="imageStyle(block)"
              :src="imageUrl(block)"
              :alt="block.c?.name || '图片'"
              loading="lazy"
              @error="onImageError(index)"
              @click="zoomable(block) && openImage(imageUrl(block))"
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
  <Teleport to="body">
    <div v-if="viewerSrc" class="image-viewer" @click="closeViewer">
      <img :src="viewerSrc" alt="图片预览" />
    </div>
  </Teleport>
</template>
