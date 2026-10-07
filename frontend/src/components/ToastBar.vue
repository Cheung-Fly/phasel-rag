<script setup>
/* ============================================================
   ToastBar —— 页面级轻提示
   全局只有一条，位置固定在底部居中（样式见 .toast）。
   状态来自 useToast 的单例，因此任何组件触发都能显示在这里。
   ============================================================ */
import { useToast } from '../composables/useToast.js';

const { message, kind, visible } = useToast();
</script>

<template>
  <div v-if="visible" class="toast show" :class="{ error: kind === 'error' }">{{ message }}</div>
</template>
