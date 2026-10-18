<script setup>
/* ============================================================
   ChatComposer —— 底部输入区
   ------------------------------------------------------------
   三件事：
     1) Enter 发送 / Shift+Enter 换行（聊天界面的通用约定）
     2) 输入框随内容自动长高，上限 180px 后转为内部滚动
     3) 响应来自其它地方的「聚焦输入框」请求

   ★ 为什么用 <textarea> 而不是 contenteditable：
     textarea 的取值、光标、粘贴、输入法行为都由浏览器处理好了；
     contenteditable 要自己处理换行、富文本粘贴、光标位置，
     在本项目里没有任何收益。

   ★ 输入法（中文）注意：Enter 在候选词未上屏时也会触发 keydown，
     但此时 e.isComposing 为 true。不排除掉的话，用拼音输入时
     一按回车选词就会把半截拼音直接发出去。这里显式跳过。
   ============================================================ */
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { useChat } from '../composables/useChat.js';
import { useComposer } from '../composables/useComposer.js';
import { useAssistantMode } from '../composables/useAssistantMode.js';
import { useSites } from '../composables/useSites.js';
import { useHealth } from '../composables/useHealth.js';

const { send, stop, busy } = useChat();
const { focusTick } = useComposer();
const assistantMode = useAssistantMode();
const { sites } = useSites();
const { knowledgeTabLabel } = useHealth();

// 个人助手侧的输入提示。
// ★ 不再写死某一所学校：站点是用户自己登记的，提示语就从已登记的站点里取。
//   这既避免了「界面示例说东软、用户配的是别的站」这种错位，也顺带告诉用户
//   「你登记的那个站点现在真的能这么用了」。
const studentHint = computed(() => {
  const site = sites.value.find((item) => item.enabled && (item.aliases || []).length);
  const alias = site && site.aliases[0];
  return alias
    ? `例如：帮我登录${alias} · Enter 发送`
    : '例如：帮我查一下系统状态 · Enter 发送';
});

// 提示语整体在脚本里算好，模板里只做一次绑定：
// 放进模板的模板字符串里就得依赖编译器把 ref 解包，不如这里明确。
const placeholderText = computed(() =>
  assistantMode.value === 'school'
    ? `${knowledgeTabLabel.value} · Enter 发送 · Shift+Enter 换行`
    : studentHint.value,
);

const draft = ref('');
const ta = ref(null);

const MAX_H = 180;

function autoGrow() {
  const el = ta.value;
  if (!el) return;
  el.style.height = 'auto';                       // 先复位，否则只会越来越矮不回去
  el.style.height = Math.min(el.scrollHeight, MAX_H) + 'px';
}

function submit() {
  const q = draft.value;
  if (busy.value || !q.trim()) return;
  draft.value = '';
  nextTick(autoGrow);
  send(q);
}

function onKeydown(e) {
  if (e.key !== 'Enter' || e.shiftKey) return;
  // 中文输入法组词过程中按回车是在「选词」，不是「发送」
  if (e.isComposing) return;
  e.preventDefault();
  submit();
}

// 外部请求聚焦（启动时 / 问答结束后 / Ctrl+K）
watch(focusTick, async () => {
  await nextTick();
  const el = ta.value;
  if (!el) return;
  el.focus();
  el.select();
});

onMounted(autoGrow);
</script>

<template>
  <form class="composer" @submit.prevent="submit">
    <textarea
      id="question"
      ref="ta"
      v-model="draft"
      rows="1"
      :placeholder="placeholderText"
      @keydown="onKeydown"
      @input="autoGrow"
    ></textarea>

    <button
      v-if="busy"
      type="button"
      class="btn-stop"
      title="停止生成 (Esc)"
      @click="stop"
    >停止</button>

    <button type="submit" id="btnSend" :disabled="busy">发送</button>
  </form>
</template>
