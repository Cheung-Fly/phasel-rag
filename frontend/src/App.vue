<script setup>
/* ============================================================
   App —— 布局外壳 + 应用级初始化
   ------------------------------------------------------------
   组件树：
     App
      ├─ AppSidebar（品牌 / 上传 / 文档 / 会话 / 健康）
      ├─ ChatPanel（头部 / 消息区 / 输入区）
      └─ ToastBar（全局提示）

   为什么初始化放在本组件的 onMounted、而不是 main.js：
     初始化里要做「恢复上次会话」「聚焦输入框」「滚动到最新」这类
     依赖真实 DOM 的事。main.js 里此时组件还没挂载，
     DOM 引用全是 null。放到 onMounted 才保证节点已就位。

   为什么快捷键在这里监听（而不是各组件里）：
     它们作用域是整个页面，且会跨组件（Ctrl+K 是给别人发信号、
     Esc 是去中断 useChat 的请求）。挂在 window 上一处处理，
     比分散到三个组件里更容易看清「到底绑了哪些键」。
   ============================================================ */
import { onMounted, onUnmounted, ref } from 'vue';
import AppSidebar from './components/AppSidebar.vue';
import ChatPanel from './components/ChatPanel.vue';
import ToastBar from './components/ToastBar.vue';

import { useTheme } from './composables/useTheme.js';
import { useHealth } from './composables/useHealth.js';
import { useDocuments } from './composables/useDocuments.js';
import { useSessions } from './composables/useSessions.js';
import { useChat } from './composables/useChat.js';
import { useComposer } from './composables/useComposer.js';
import { getCurrent } from './lib/storage.js';

// 窄屏抽屉的开关状态。只在 App 层持有，向下传给侧栏。
const sidebarOpen = ref(false);

const { apply: applyTheme, toggle: toggleTheme } = useTheme();
const { start: startHealth, stop: stopHealth } = useHealth();
const { refresh: refreshDocs } = useDocuments();
const { sync } = useSessions();
const { busy, stop: stopGenerating, openSession, showWelcome } = useChat();
const { requestFocus } = useComposer();

function onKeydown(e) {
  // Ctrl/Cmd + K：聚焦输入框
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    requestFocus();
    return;
  }

  // Ctrl/Cmd + /：切换主题
  if ((e.ctrlKey || e.metaKey) && e.key === '/') {
    e.preventDefault();
    toggleTheme();
    return;
  }

  // Esc：停止生成。只在真的在生成时才拦，
  // 否则会吃掉其它（将来可能有的）Esc 语义。
  if (e.key === 'Escape' && busy.value) {
    stopGenerating();
  }
}

onMounted(async () => {
  applyTheme();                                  // 对齐 DOM 与当前偏好
  window.addEventListener('keydown', onKeydown);

  // 三件事并行发起没有意义 —— 都很快，但顺序影响观感：
  // 先把文档列表和健康状态铺好（用户一进来就能看到「有哪些文档」），
  // 再去决定对话区要显示历史还是欢迎语。
  await refreshDocs();
  startHealth();

  const list = await sync();

  // 恢复上次的会话。两个条件都要满足：
  //   1) 本地记着一个 session_id；
  //   2) 它确实还在服务器返回的列表里 —— 否则会去请求一个已删除的会话，
  //      用户看到的是空的对话区，会以为数据丢了。
  const saved = getCurrent();
  if (saved && list.some((s) => s.id === saved)) {
    await openSession(saved);
  } else {
    showWelcome();
  }

  requestFocus();
});

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown);
  // 停止轮询，避免组件销毁后定时器还在打接口（开发期热更新时尤其明显）
  stopHealth();
});
</script>

<template>
  <!-- 窄屏侧栏开关。宽屏下由 CSS 隐藏（媒体查询里才 display:block） -->
  <button class="btn-menu" title="显示/隐藏侧栏" @click="sidebarOpen = !sidebarOpen">☰</button>

  <div class="layout">
    <AppSidebar :open="sidebarOpen" />
    <ChatPanel @close-sidebar="sidebarOpen = false" />
  </div>

  <ToastBar />
</template>
