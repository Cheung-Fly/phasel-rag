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
import TaskPanel from './TaskPanel.vue';
import SitePanel from './SitePanel.vue';
import ApprovalPanel from './ApprovalPanel.vue';
import AuditTimelinePanel from './AuditTimelinePanel.vue';
import SuggestionPanel from './SuggestionPanel.vue';
import SessionList from './SessionList.vue';
import HealthStatus from './HealthStatus.vue';
import ExtensionPanel from './ExtensionPanel.vue';
import ModelPanel from './ModelPanel.vue';

defineProps({
  open: { type: Boolean, default: false },
  assistantMode: { type: String, default: 'school' },
});
</script>

<template>
  <aside class="sidebar" :class="{ open }">
    <BrandBar />
    <ModelPanel />
    <ExtensionPanel />
    <template v-if="assistantMode === 'school'">
      <UploadPanel />
      <DocumentList />
      <!-- 后台任务紧跟文档列表：上传后「它跑到哪一步了」是紧接着的下一个问题。
           面板在没有任务、执行器也正常时会自行收起（见 TaskPanel 注释）。 -->
      <TaskPanel />
    </template>
    <!-- 个人助手侧：助手能操作哪些站点（阶段 C ①）。
         放在这里而不是学校侧：站点注册表只在个人助手模式下参与判断，
         「登录 XX」这句话本身也只在个人助手里才有意义。 -->
    <template v-else>
      <SitePanel />
      <!-- 主动建议放最上：它是「助手基于记忆想主动提醒你的事」，
           比「等你点头的审批」和「已经做过的事」都更该先看到（阶段 D ②）。 -->
      <SuggestionPanel />
      <!-- 待批准操作放在审计时间线**上方**：先看到「等我点头的事」，
           再看「已经做过的事」。二者一前一后，构成完整的信任闭环。 -->
      <ApprovalPanel />
      <!-- 紧随其后：回答的都是「助手在校园里做过/能做什么」，
           时间线里的操作事件大多源自站点上的点击（阶段 C ④）。 -->
      <AuditTimelinePanel />
    </template>
    <SessionList />
    <HealthStatus />
    <div class="sidebar-foot">单击会话打开 · 双击导出 · Ctrl+K 聚焦输入</div>
  </aside>
</template>
