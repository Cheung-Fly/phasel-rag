<script setup>
/* ============================================================
   SessionList —— 会话历史列表
   ------------------------------------------------------------
   三种操作：
     单击  打开（载入这段会话的历史消息）
     双击  导出为 Markdown
     悬停出现「改 / 删」两个小按钮

   ★ 操作按钮上必须 .stop：
     它们嵌在可点击的行里。不阻止冒泡的话，点「删」会先触发行的
     单击处理 —— 于是删之前先把它打开了，一次点击做两件事，
     而且打开动作还会去请求一个即将被删掉的会话。
     迁移前是用事件委托 + 判断 data-op 实现的，用 .stop 更直白。
   ============================================================ */
import { useSessions } from '../composables/useSessions.js';
import { useChat } from '../composables/useChat.js';
import { useToast } from '../composables/useToast.js';
import { fmtTime } from '../lib/storage.js';

const { sessions, currentId, rename, clearAllRemote, sync, exportSession } = useSessions();
const { openSession, deleteSession, newChat } = useChat();
const { toast } = useToast();

function onClick(id) {
  if (id === currentId.value) return;   // 已经是当前会话，不必重新拉一遍
  openSession(id);
}

async function onDblClick(id) {
  try {
    await exportSession(id);
    toast('已导出为 Markdown');
  } catch (e) {
    toast('导出失败：' + e.message, 'error');
  }
}

function onRename(s) {
  const name = prompt('给这段会话起个名字：', s.title || '');
  if (name === null) return;            // 用户取消
  rename(s.id, name);
}

async function onClearAll() {
  if (!confirm('清空所有会话记忆？知识库文档不受影响。')) return;
  try {
    const n = await clearAllRemote();
    newChat();
    await sync();
    toast(`已清空 ${n} 段会话`);
  } catch (e) {
    toast('清空失败：' + e.message, 'error');
  }
}
</script>

<template>
  <section class="panel grow">
    <h3 class="panel-title">
      会话历史
      <button class="link-btn" title="清空所有会话记忆" @click="onClearAll">全清</button>
    </h3>

    <ul class="session-list">
      <li v-if="!sessions.length" class="empty">还没有会话</li>
      <li
        v-for="s in sessions"
        :key="s.id"
        class="session-item"
        :class="{ active: s.id === currentId }"
        @click="onClick(s.id)"
        @dblclick="onDblClick(s.id)"
      >
        <div class="s-title" :title="s.title">{{ s.title }}</div>
        <div class="s-meta">{{ fmtTime(s.ts) }}<span v-if="s.turns"> · {{ s.turns }} 轮</span></div>
        <div class="s-ops">
          <button class="s-op" title="重命名" @click.stop="onRename(s)">改</button>
          <button class="s-op" title="删除这段会话" @click.stop="deleteSession(s.id)">删</button>
        </div>
      </li>
    </ul>
  </section>
</template>
