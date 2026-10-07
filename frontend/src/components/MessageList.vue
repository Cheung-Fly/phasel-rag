<script setup>
/* ============================================================
   MessageList —— 消息滚动区
   ------------------------------------------------------------
   ★ 自动滚动的分寸（这是个容易做糙的细节）：
     新消息进来要滚到底；但流式输出过程中**只在用户本来就贴着底部时**才滚。
     否则用户想往上翻看前面的内容，会被每个 token 强行拽回底部 ——
     这在长回答里几乎等于禁止回看。

   ★ 为什么用 watch(scrollReq) 而不是 watch(messages)：
     数据层（useChat）不该碰 DOM，所以它只发一个自增的「请滚一下」信号，
     由本组件决定滚不滚。如果监听 messages 数组本身，
     流式追加每帧都会触发深比较，白白消耗性能。
   ============================================================ */
import { nextTick, ref, watch } from 'vue';
import { useChat } from '../composables/useChat.js';
import MessageBubble from './MessageBubble.vue';

const emit = defineEmits(['close-sidebar']);

const { messages, scrollReq, regenerate } = useChat();

const box = ref(null);

// 距底部多少像素以内算「贴着底部」
const NEAR_BOTTOM_PX = 120;

watch(
  () => scrollReq.value.tick,
  async () => {
    await nextTick();                  // 等 DOM 更新完再量高度，否则量到旧值
    const el = box.value;
    if (!el) return;

    const { force } = scrollReq.value;
    const nearBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;

    if (force || nearBottom) el.scrollTop = el.scrollHeight;
  },
);
</script>

<template>
  <!-- 点消息区收起侧栏：窄屏下侧栏是盖住内容的抽屉，
       看完会话列表点回正文时，抽屉理应自己退开。这是移动端的基本手感。 -->
  <div ref="box" class="messages" @click="emit('close-sidebar')">
    <MessageBubble
      v-for="(m, i) in messages"
      :key="m.id"
      :msg="m"
      :index="i"
      @regenerate="regenerate"
    />
  </div>
</template>
