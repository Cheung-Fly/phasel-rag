<script setup>
/* ============================================================
   ApprovalPanel —— 「待批准操作」侧栏面板（阶段 C ② 的界面收口）
   ------------------------------------------------------------
   审批卡片挂在消息上、只在个人助手模式下出现，而且 90 秒过期。
   如果用户滚远了、或切到别的会话，一条「等待批准」的操作就会被
   淹没，直到过期都无人处理。这个面板给了它一个不随滚动消失的入口。

   形态取舍：侧栏只预览「待处理」的最近几条（pending/approved），
   已落定的（executed/rejected/expired/failed）不作为行动项展示 ——
   那些是「结果」，去看审计时间线更合适。这里的职责是「催你点头」。
   ============================================================ */
import { computed, onMounted } from 'vue';
import { useApprovals } from '../composables/useApprovals.js';

const {
  approvals,
  loading,
  error,
  loaded,
  acting,
  refresh,
  approveAndExecute,
  reject,
} = useApprovals();

/** 只有还「需要行动」的才算待批准：pending（等批准）、approved（批准了还没执行）。 */
const pending = computed(() =>
  approvals.value.filter((a) => a.status === 'pending' || a.status === 'approved'));

/** 判断一条审批是终端命令还是网页点击。 */
const isShell = (a) => a.action === 'run_shell';

/** 人话翻译（终端命令才有）。 */
const summaryOf = (a) => (a.detail && typeof a.detail.summary === 'string') ? a.detail.summary : '';

/** 命令/目标文本，可能很长，截断显示。 */
function shortTarget(a) {
  const t = a.target || '';
  return t.length > 60 ? `${t.slice(0, 60)}…` : t;
}

onMounted(refresh);
</script>

<template>
  <section class="panel">
    <h3 class="panel-title">
      待批准操作
      <span class="count">{{ pending.length }}</span>
      <button class="link-btn" type="button" :disabled="loading" @click="refresh">
        {{ loading ? '读取中…' : '刷新' }}
      </button>
    </h3>

    <ul class="approval-list">
      <li v-if="error" class="empty">读取失败：{{ error }}</li>
      <li v-else-if="!loaded" class="empty">加载中…</li>
      <li v-else-if="!pending.length" class="empty">没有待批准的操作</li>
      <li v-else>
        <div
          v-for="a in pending.slice(0, preview)"
          :key="a.approval_id"
          class="approval-item"
        >
          <div class="approval-kind">
            {{ isShell(a) ? '终端命令' : '网页点击' }}
            <span class="approval-badge" :class="a.status">{{ a.status === 'approved' ? '待执行' : '待批准' }}</span>
          </div>
          <code class="approval-cmd">{{ shortTarget(a) }}</code>
          <p v-if="isShell(a) && summaryOf(a)" class="approval-summary">{{ summaryOf(a) }}</p>
          <div class="approval-item-actions">
            <button
              type="button"
              class="btn-confirm"
              :disabled="acting !== ''"
              @click="approveAndExecute(a.approval_id)"
            >{{ acting === a.approval_id ? '处理中…' : '批准并执行' }}</button>
            <button
              type="button"
              class="btn-ghost"
              :disabled="acting !== ''"
              @click="reject(a.approval_id)"
            >拒绝</button>
          </div>
        </div>
      </li>
    </ul>

    <p class="approval-hint">
      这些操作 90 秒后自动作废。处理结果见「助手做过什么」。
    </p>
  </section>
</template>

<style scoped>
.approval-list { list-style: none; margin: 0; padding: 0; }
.approval-list > li { margin: 0; }
.approval-list li.empty {
  padding: 6px 8px;
  color: var(--text-faint);
  font-size: 11.5px;
  text-align: center;
}
.approval-item {
  padding: 8px;
  margin-bottom: 6px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--warn) 5%, transparent);
}
.approval-kind {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 11px; font-weight: 600;
  color: var(--text-dim);
}
.approval-badge {
  font-size: 10px; font-weight: 500;
  padding: 1px 6px; border-radius: 8px;
  background: var(--warn); color: #fff;
}
.approval-badge.approved { background: var(--accent); }
.approval-cmd {
  display: block; margin-top: 5px;
  font-size: 11.5px; font-family: ui-monospace, Consolas, "Courier New", monospace;
  color: var(--text);
  word-break: break-all;
}
.approval-summary {
  margin: 4px 0 0; font-size: 11px; color: var(--text-dim);
}
.approval-item-actions { display: flex; gap: 6px; margin-top: 8px; }
.approval-item-actions .btn-confirm {
  padding: 5px 10px; font-size: 11.5px;
  border: 0; border-radius: var(--radius-sm);
  background: var(--accent); color: #fff; cursor: pointer;
}
.approval-item-actions .btn-confirm:disabled { opacity: .5; cursor: not-allowed; }
.approval-item-actions .btn-ghost {
  padding: 5px 10px; font-size: 11.5px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: transparent; color: var(--text-dim); cursor: pointer;
}
.approval-item-actions .btn-ghost:disabled { opacity: .5; cursor: not-allowed; }
.approval-hint { margin: 0; font-size: 10.5px; color: var(--text-faint); }
</style>
