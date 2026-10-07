<script setup>
/* ============================================================
   AppSidebar —— 左侧栏容器
   ------------------------------------------------------------
   窄屏时它变成抽屉：默认移出屏幕，拿到 open 类才滑入。
   这个类由 App.vue 控制（点 ☰ 按钮或点消息区都会切换）。
   ============================================================ */
import BrandBar from './BrandBar.vue';
import UploadPanel from './UploadPanel.vue';
import DocumentList from './DocumentList.vue';
import SessionList from './SessionList.vue';
import HealthStatus from './HealthStatus.vue';

defineProps({
  open: { type: Boolean, default: false },
});
</script>

<template>
  <aside class="sidebar" :class="{ open }">
    <BrandBar />
    <UploadPanel />
    <DocumentList />
    <SessionList />
    <HealthStatus />
    <div class="sidebar-foot">单击会话打开 · 双击导出 · Ctrl+K 聚焦输入</div>
  </aside>
</template>
