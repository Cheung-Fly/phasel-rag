<script setup>
/* ============================================================
   SourceList —— 回答下方的「引用片段」
   ------------------------------------------------------------
   用原生 <details>/<summary> 而不是自己写展开逻辑：
   浏览器自带展开状态、键盘可访问（Enter/Space）、屏幕阅读器能读，
   而且不需要维护 isOpen 状态。
   ============================================================ */
defineProps({
  sources: { type: Array, required: true },
});
</script>

<template>
  <details class="sources">
    <summary>引用 {{ sources.length }} 个片段</summary>
    <div class="source-list">
      <div v-for="s in sources" :key="s.index" class="source">
        <span class="source-idx">[{{ s.index }}]</span>
        <span class="source-body">
          <!-- 文件名和摘要都是文档里的原始内容，用插值输出即为纯文本，
               浏览器不会把其中的尖括号当标签解析（Vue 自动转义）。 -->
          <span class="source-file">
            {{ s.filename || '未知' }}
            <span class="score">{{ s.score }}</span>
          </span>
          <span class="source-preview">{{ s.preview || '' }}</span>
        </span>
      </div>
    </div>
  </details>
</template>
