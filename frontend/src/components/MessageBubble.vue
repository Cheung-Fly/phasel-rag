<script setup>
/* ============================================================
   MessageBubble —— 单条消息
   ------------------------------------------------------------
   用户消息与助手消息的渲染方式**刻意不同**：
     用户输入 → 纯文本插值（{{ }}）。
       用户可能输入 <script> 或 Markdown 语法，但那是他自己写的内容，
       按原样显示才符合预期；当成 Markdown 渲染反而会「吃掉」字符。
     助手回答 → v-html，走 lib/markdown.js。
       模型输出 Markdown，需要渲染。安全性由 markdown.js 保证 ——
       它在做任何解析之前先把 & < > " ' 全部转义，
       所以内容里的 HTML 不会变成真实标签。这也是本项目不引入
       marked / markdown-it 的原因：自己写的这 200 行可控且无依赖。

   工具条（复制 / 重新生成 / 耗时）只在「非错误、非流式中」出现，
   与原实现一致 —— 正在逐字输出时按钮既没用也会跳来跳去。
   ============================================================ */
import { computed, ref } from 'vue';
import { render } from '../lib/markdown.js';
import { useToast } from '../composables/useToast.js';
import SourceList from './SourceList.vue';

const props = defineProps({
  msg: { type: Object, required: true },
  index: { type: Number, required: true },
});

const emit = defineEmits(['regenerate']);

const { toast } = useToast();

const isUser = computed(() => props.msg.role === 'user');

/** 只有助手消息需要渲染 Markdown；computed 会随 msg.text 变化自动重算 */
const rendered = computed(() => (isUser.value ? '' : render(props.msg.text)));

const showTools = computed(() =>
  !isUser.value && !props.msg.error && !props.msg.streaming);

/* ---------- 复制 ---------- */
const copied = ref(false);
let copyTimer = null;

function flashCopied() {
  copied.value = true;
  clearTimeout(copyTimer);
  copyTimer = setTimeout(() => { copied.value = false; }, 1200);
}

async function copy() {
  const text = props.msg.text;
  try {
    await navigator.clipboard.writeText(text);
    flashCopied();
  } catch {
    // Clipboard API 只在安全上下文（https / localhost）可用。
    // 本项目是 http + 端口转发访问，很可能拿不到，所以必须有退路：
    // 造一个临时 textarea、选中、用已废弃但广泛支持的 execCommand 复制。
    const ta = document.createElement('textarea');
    ta.value = text;
    // 放到视口外，避免选中时页面跳动
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      flashCopied();
    } catch {
      toast('复制失败', 'error');
    }
    ta.remove();
  }
}
</script>

<template>
  <div class="msg" :class="`msg-${msg.role}`">
    <div class="bubble" :class="{ error: msg.error }">

      <!-- 助手：Markdown 渲染 + 流式光标（光标是 .streaming 的伪元素） -->
      <div
        v-if="!isUser"
        class="md-body"
        :class="{ streaming: msg.streaming }"
        v-html="rendered"
      ></div>

      <!-- 用户：原样纯文本，CSS 用 white-space: pre-wrap 保留换行 -->
      <div v-else class="md-body">{{ msg.text }}</div>

      <SourceList v-if="msg.sources && msg.sources.length" :sources="msg.sources" />

      <div v-if="showTools" class="msg-tools">
        <button class="tool" title="复制回答内容" @click="copy">
          {{ copied ? '已复制' : '复制' }}
        </button>
        <button class="tool" title="用同一个问题再问一次" @click="emit('regenerate', index)">
          重新生成
        </button>
        <span
          v-if="msg.meta"
          class="tool-meta"
          title="首字延迟 / 总耗时 / 分块数"
        >{{ msg.meta }}</span>
      </div>
    </div>
  </div>
</template>
