<script setup>
/* ============================================================
   ChatPanel —— 右侧对话区
   ============================================================ */
import { computed } from 'vue';
import { useChat } from '../composables/useChat.js';
import { useSessions } from '../composables/useSessions.js';
import MessageList from './MessageList.vue';
import ChatComposer from './ChatComposer.vue';

defineEmits(['close-sidebar']);

const { newChat } = useChat();
const { currentId } = useSessions();

// 会话 ID 太长，界面上只显示前 8 位；没有会话时显示占位符
const tag = computed(() => (currentId.value ? currentId.value.slice(0, 8) : '—'));
</script>

<template>
  <main class="chat">
    <header class="chat-header">
      <div class="chat-title">知识库问答</div>
      <div class="chat-actions">
        <span class="session-tag" title="当前会话 ID">{{ tag }}</span>
        <button class="btn-ghost" title="开一个新会话" @click="newChat">新会话</button>
      </div>
    </header>

    <MessageList @close-sidebar="$emit('close-sidebar')" />
    <ChatComposer />
  </main>
</template>
