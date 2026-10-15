<script setup>
/* ============================================================
   SuggestionPanel —— 「主动建议」侧栏面板（阶段 D ② 的界面收口）
   ------------------------------------------------------------
   助手基于长期记忆（user_facts）推断出「现在该做的事」，在这里
   以卡片形式列出，等你决定：采纳（转后台任务）或忽略（不再提示）。

   形态取舍：只展示待决定的建议（pending）。采纳/忽略后的结果是
   「后台任务」与「记忆」的范畴，去看任务面板即可，不在这里冗余。
   ============================================================ */
import { computed, onMounted } from 'vue';
import { useSuggestions } from '../composables/useSuggestions.js';

const { suggestions, loading, error, loaded, acting, refresh, accept, dismiss } =
  useSuggestions();

/** 类别中文标签。 */
const kindLabel = {
  study: '学习',
  job: '求职',
  health: '健康',
  life: '生活',
};

onMounted(refresh);
</script>

<template>
  <section class="panel">
    <h3 class="panel-title">
      主动建议
      <span class="count">{{ suggestions.length }}</span>
      <button class="link-btn" type="button" :disabled="loading" @click="refresh">
        {{ loading ? '思考中…' : '刷新' }}
      </button>
    </h3>

    <ul class="suggestion-list">
      <li v-if="error" class="empty">读取失败：{{ error }}</li>
      <li v-else-if="!loaded" class="empty">加载中…</li>
      <li v-else-if="!suggestions.length" class="empty">暂无建议</li>
      <li v-else>
        <div
          v-for="s in suggestions"
          :key="s.id"
          class="suggestion-item"
        >
          <div class="suggestion-kind">
            {{ kindLabel[s.kind] || s.kind }}
          </div>
          <p class="suggestion-text">{{ s.suggestion }}</p>
          <p v-if="s.reason" class="suggestion-reason">{{ s.reason }}</p>
          <div class="suggestion-item-actions">
            <button
              type="button"
              class="btn-confirm"
              :disabled="acting !== ''"
              @click="accept(s.id)"
            >{{ acting === s.id ? '处理中…' : '采纳' }}</button>
            <button
              type="button"
              class="btn-ghost"
              :disabled="acting !== ''"
              @click="dismiss(s.id)"
            >忽略</button>
          </div>
        </div>
      </li>
    </ul>

    <p class="suggestion-hint">
      采纳后会转成后台任务执行；忽略后 7 天内不再提示同类建议。
    </p>
  </section>
</template>

<style scoped>
.suggestion-list { list-style: none; margin: 0; padding: 0; }
.suggestion-list > li { margin: 0; }
.suggestion-list li.empty {
  padding: 6px 8px;
  color: var(--text-faint);
  font-size: 11.5px;
  text-align: center;
}
.suggestion-item {
  padding: 8px;
  margin-bottom: 6px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--accent) 5%, transparent);
}
.suggestion-kind {
  font-size: 11px; font-weight: 600;
  color: var(--text-dim);
}
.suggestion-text {
  margin: 4px 0 0; font-size: 12px; color: var(--text);
  line-height: 1.5;
}
.suggestion-reason {
  margin: 3px 0 0; font-size: 10.5px; color: var(--text-faint);
}
.suggestion-item-actions { display: flex; gap: 6px; margin-top: 8px; }
.suggestion-item-actions .btn-confirm {
  padding: 5px 10px; font-size: 11.5px;
  border: 0; border-radius: var(--radius-sm);
  background: var(--accent); color: #fff; cursor: pointer;
}
.suggestion-item-actions .btn-confirm:disabled { opacity: .5; cursor: not-allowed; }
.suggestion-item-actions .btn-ghost {
  padding: 5px 10px; font-size: 11.5px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: transparent; color: var(--text-dim); cursor: pointer;
}
.suggestion-item-actions .btn-ghost:disabled { opacity: .5; cursor: not-allowed; }
.suggestion-hint { margin: 0; font-size: 10.5px; color: var(--text-faint); }
</style>
