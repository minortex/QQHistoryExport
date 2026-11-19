<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ConversationRow } from '../api'

const props = defineProps<{
  rows: ConversationRow[]
  currentKey: string
  scanning: boolean
}>()
const emit = defineEmits<{ select: [string]; scan: [] }>()

const keyword = ref('')

const filtered = computed(() =>
  props.rows.filter((row) =>
    `${row.name} ${row.uin}`.toLowerCase().includes(keyword.value.toLowerCase()),
  ),
)
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-inner">
      <div class="side-head">
        <span class="side-title">会话 {{ rows.length }}</span>
        <button
          class="icon-btn"
          :disabled="scanning"
          title="扫描会话（每次都会重新读取设备）"
          @click="emit('scan')"
        >
          {{ scanning ? '…' : '⟳' }}
        </button>
      </div>
      <div class="search">
        <input v-model="keyword" placeholder="搜索会话名或号码" />
      </div>

      <div class="list">
        <div
          v-for="row in filtered"
          :key="row.key"
          class="conv library"
          :class="{ active: row.key === currentKey, disabled: !row.resolved }"
          tabindex="0"
          @click="row.resolved && emit('select', row.key)"
          @keydown.enter="row.resolved && emit('select', row.key)"
        >
          <span class="conv-main">
            <span class="conv-name">{{ row.name }}</span>
            <span class="conv-meta">
              {{ row.kind === 'troop' ? '群' : '好友' }} ·
              {{ row.total.toLocaleString() }} 条
            </span>
          </span>
          <span
            v-if="!row.resolved"
            class="badge danger"
            title="Friends/TroopInfoV2 中找不到该 QQ 号，解析器无法定位会话"
          >
            无法定位
          </span>
        </div>
        <p v-if="!filtered.length" class="muted empty">
          <template v-if="scanning">正在读取会话列表…</template>
          <template v-else-if="rows.length">没有匹配的会话</template>
          <template v-else>
            还没有会话。<button class="primary small" @click="emit('scan')">扫描</button>
          </template>
        </p>
      </div>
    </div>
  </aside>
</template>
