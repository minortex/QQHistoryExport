<script setup lang="ts">
import { reactive, ref } from 'vue'
import { api } from '../api'
import type { ConfigView } from '../api'

const props = defineProps<{ config: ConfigView; closable: boolean }>()
const emit = defineEmits<{ saved: [ConfigView]; close: [] }>()

const form = reactive({
  dbPath: props.config.dbPath,
  dbstPath: props.config.dbstPath,
  kcPath: '',
  key: '',
  mediaRoot: props.config.mediaRoot,
  imagesPath: props.config.imagesPath,
  voicePath: props.config.voicePath,
  videoPath: props.config.videoPath,
  needImages: props.config.needImages,
  needVoice: props.config.needVoice,
  needVideo: props.config.needVideo,
  needAvatars: props.config.needAvatars,
  adbSerial: props.config.adbSerial,
  adbSqlite: props.config.adbSqlite,
})
const busy = ref(false)
const error = ref('')

async function submit() {
  busy.value = true
  error.value = ''
  try {
    const result = await api.setup({ ...form })
    emit('saved', result.config)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="overlay">
    <form class="panel setup" @submit.prevent="submit">
      <header>
        <h2>连接 QQ 数据</h2>
        <button v-if="closable" type="button" class="ghost" @click="emit('close')">关闭</button>
      </header>
      <p class="muted">
        数据库路径必须是解密后的副本（例如从手机上取出的 <code>{{ config.selfQQ || 'QQ号' }}.db</code>
        与 <code>slowtable_QQ号.db</code>），服务端只读访问。
        也可以填 <code>adb:/data/user/0/com.tencent.mobileqq/databases/QQ号.db</code>
        通过 adb 在线读取（导入时只拉取所选会话的紧凑数据）。
      </p>

      <label>
        主库 db 路径
        <input v-model="form.dbPath" placeholder="/path/to/QQ号.db" required />
      </label>
      <label>
        slowtable 路径（可选）
        <input v-model="form.dbstPath" placeholder="/path/to/slowtable_QQ号.db" />
      </label>
      <label>
        kc 密钥文件（可选，优先于手动密钥）
        <input v-model="form.kcPath" placeholder="/path/to/kc" />
      </label>
      <label>
        或直接填写密钥
        <input v-model="form.key" type="password" :placeholder="config.hasKey ? '已设置，留空不变' : 'kc 文件内容'" />
      </label>
      <label>
        媒体根目录（可选，自动推导 chatpic/shortvideo/ptt）
        <input v-model="form.mediaRoot" placeholder="/path/to/Tencent/MobileQQ 的上级目录" />
      </label>
      <details>
        <summary class="muted">单独指定媒体目录</summary>
        <label>
          聊天图片目录（chatimg）
          <input v-model="form.imagesPath" placeholder="/path/to/chatpic/chatimg" />
        </label>
        <label>
          语音目录（ptt）
          <input v-model="form.voicePath" placeholder="/path/to/QQ号/ptt" />
        </label>
        <label>
          视频目录（shortvideo）
          <input v-model="form.videoPath" placeholder="/path/to/shortvideo" />
        </label>
      </details>
      <details>
        <summary class="muted">adb 连接（填写 adb: 路径时使用）</summary>
        <label>
          adb 序列号（可选，留空自动选择第一台设备）
          <input v-model="form.adbSerial" placeholder="例如 adb-ee0fd8e9-kwbrNO._adb-tls-connect._tcp" />
        </label>
        <label>
          设备上的 sqlite3 路径
          <input v-model="form.adbSqlite" placeholder="/data/data/com.termux/files/usr/bin/sqlite3" />
        </label>
        <p class="muted">
          adb 模式导入时会临时在设备 /data/local/tmp 生成快照，只拉回所选会话和元数据表；
          暂不导出图片/语音/视频。
        </p>
      </details>

      <div class="checks">
        <label><input v-model="form.needImages" type="checkbox" /> 导出图片</label>
        <label><input v-model="form.needVoice" type="checkbox" /> 导出语音</label>
        <label><input v-model="form.needVideo" type="checkbox" /> 导出视频</label>
        <label><input v-model="form.needAvatars" type="checkbox" /> 拉取头像（联网）</label>
      </div>

      <p v-if="error" class="error">{{ error }}</p>
      <footer>
        <button class="primary" type="submit" :disabled="busy">
          {{ busy ? '保存中…' : '保存' }}
        </button>
      </footer>
    </form>
  </div>
</template>
