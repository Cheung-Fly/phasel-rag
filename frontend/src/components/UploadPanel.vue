<script setup>
/* ============================================================
   UploadPanel —— 上传区（点击选择 + 拖拽）+ 进度
   ------------------------------------------------------------
   ★ 为什么用 <label for="fileInput"> 而不是直接给 input 加样式：
     原生 <input type="file"> 在各浏览器里的外观无法可靠地自定义
     （内部是影子 DOM），用 label 关联隐藏 input 是唯一稳的做法。

   ★ 拖拽的三个事件必须都 preventDefault：
     否则浏览器会「打开这个文件」—— 直接把你当前页面替换掉，
     用户会以为网站崩了。其中 dragover 尤其关键：
     不阻止它，drop 根本不会触发。
   ============================================================ */
import { ref } from 'vue';
import { useDocuments } from '../composables/useDocuments.js';
import { useHealth } from '../composables/useHealth.js';

const { uploader, importingUrl, upload, importWebPage } = useDocuments();
const { maxUploadMb } = useHealth();

const dragover = ref(false);
const pageUrl = ref('');

async function submitImport() {
  if (await importWebPage(pageUrl.value)) pageUrl.value = '';
}

function pick(e) {
  const f = e.target.files && e.target.files[0];
  // 立刻清空 input：否则连续两次选同一个文件时不会触发 change 事件
  // （值没变，浏览器认为没有变化）。
  e.target.value = '';
  if (f) upload(f);
}

function onDrop(e) {
  dragover.value = false;
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) upload(f);
}
</script>

<template>
  <section class="panel">
    <h3 class="panel-title">知识库</h3>

    <label
      class="dropzone"
      :class="{ dragover }"
      for="fileInput"
      @dragenter.prevent="dragover = true"
      @dragover.prevent="dragover = true"
      @dragleave.prevent="dragover = false"
      @drop.prevent="onDrop"
    >
      <span class="dz-icon">＋</span>
      <span class="dz-text">点击选择，或拖文件到这里</span>
      <!-- 上限取自后端 /health，不写死（见 useHealth 注释） -->
      <span class="dz-hint">.md / .txt / .pdf / .docx / .pptx / .xlsx / .html · 最大 {{ maxUploadMb }}MB</span>
    </label>

    <input type="file" id="fileInput" accept=".md,.markdown,.txt,.pdf,.docx,.pptx,.xlsx,.html,.htm" hidden @change="pick">

    <form class="url-import" @submit.prevent="submitImport">
      <input
        v-model="pageUrl"
        type="url"
        required
        maxlength="2048"
        placeholder="https:// 公开网页地址"
        aria-label="要导入的公开网页地址"
      >
      <button type="submit" class="btn-ghost" :disabled="importingUrl">
        {{ importingUrl ? '正在读取…' : '导入网页' }}
      </button>
    </form>
    <p class="url-import-hint">需启用 SearXNG 网页读取 MCP；仅接受公开 HTTPS 页面。</p>

    <div v-if="uploader.active" class="upload-progress">
      <div class="up-name">{{ uploader.name }}</div>
      <div class="up-bar"><div class="up-bar-fill" :style="{ width: uploader.pct + '%' }"></div></div>
      <div class="up-status">{{ uploader.status }}</div>
    </div>
  </section>
</template>

<style scoped>
.url-import { display: flex; gap: 6px; }
.url-import input { min-width: 0; flex: 1; }
.url-import-hint { margin: 0; color: var(--text-faint); font-size: 11px; }
</style>
