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

const { uploader, upload } = useDocuments();
const { maxUploadMb } = useHealth();

const dragover = ref(false);

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
      <span class="dz-hint">.md / .txt / .pdf · 最大 {{ maxUploadMb }}MB</span>
    </label>

    <input type="file" id="fileInput" accept=".md,.markdown,.txt,.pdf" hidden @change="pick">

    <div v-if="uploader.active" class="upload-progress">
      <div class="up-name">{{ uploader.name }}</div>
      <div class="up-bar"><div class="up-bar-fill" :style="{ width: uploader.pct + '%' }"></div></div>
      <div class="up-status">{{ uploader.status }}</div>
    </div>
  </section>
</template>
