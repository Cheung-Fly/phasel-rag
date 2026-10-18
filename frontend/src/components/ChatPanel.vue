<script setup>
/* ============================================================
   ChatPanel —— 右侧对话区
   ============================================================ */
import { computed } from 'vue';
import { useChat } from '../composables/useChat.js';
import { useSessions } from '../composables/useSessions.js';
import { useHealth } from '../composables/useHealth.js';
import MessageList from './MessageList.vue';
import ChatComposer from './ChatComposer.vue';

const props = defineProps({
  assistantMode: { type: String, default: 'school' },
  busy: { type: Boolean, default: false },
});
// 2026-10-14：'logout' 事件随登录界面一并移除。单用户本地模式下没有
// 「换个身份登录」这件事，留一个点了不知道会发生什么的按钮只会造成困惑。
// 浏览器验证会话仍可关闭 —— 那是 BrowserHandoff 面板自己的
//「关闭并清除会话」按钮，与该面板生命周期绑定，比顶栏的全局退出更合理。
const emit = defineEmits(['close-sidebar', 'switch-mode']);

const { newChat } = useChat();
const { currentId } = useSessions();
const { knowledgeTabLabel, assistantTabLabel } = useHealth();

// 会话 ID 太长，界面上只显示前 8 位；没有会话时显示占位符
const tag = computed(() => (currentId.value ? currentId.value.slice(0, 8) : '—'));
</script>

<template>
  <main class="chat">
    <header class="chat-header">
      <nav class="assistant-tabs" role="tablist" aria-label="助手栏目">
        <button
          type="button"
          role="tab"
          :aria-selected="props.assistantMode === 'school'"
          :class="{ selected: props.assistantMode === 'school' }"
          :disabled="props.busy"
          @click="emit('switch-mode', 'school')"
        >{{ knowledgeTabLabel }}</button>
        <button
          type="button"
          role="tab"
          :aria-selected="props.assistantMode === 'student'"
          :class="{ selected: props.assistantMode === 'student' }"
          :disabled="props.busy"
          @click="emit('switch-mode', 'student')"
        >{{ assistantTabLabel }}</button>
      </nav>
      <div class="chat-actions">
        <span class="session-tag" title="当前会话 ID">{{ tag }}</span>
        <button class="btn-ghost" title="开一个新会话" @click="newChat">新会话</button>
      </div>
    </header>

    <MessageList @close-sidebar="$emit('close-sidebar')" />
    <ChatComposer />
  </main>
</template>

<style scoped>
.assistant-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-right: auto;
  padding: 3px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
}
.assistant-tabs button {
  padding: 7px 12px;
  border: 0;
  border-radius: calc(var(--radius-sm) - 2px);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}
.assistant-tabs button.selected {
  background: var(--bg-panel);
  color: var(--text);
  box-shadow: var(--shadow);
}
.assistant-tabs button:disabled { cursor: wait; opacity: .6; }
@media (max-width: 560px) {
  .assistant-tabs { margin-right: 0; }
  .assistant-tabs button { padding: 6px 8px; }
}
</style>
