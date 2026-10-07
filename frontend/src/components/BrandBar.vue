<script setup>
/* ============================================================
   BrandBar —— 侧栏顶部的标题与主题切换按钮
   ============================================================ */
import { useTheme } from '../composables/useTheme.js';

const { icon, title, toggle } = useTheme();
</script>

<template>
  <div class="brand">
    <div class="brand-text">
      <div class="brand-title">私有知识库</div>
      <div class="brand-sub">阶段一 · RAG</div>
    </div>
    <button class="icon-btn" :title="`${title} (Ctrl+/)`" @click="toggle">{{ icon }}</button>
  </div>
</template>
