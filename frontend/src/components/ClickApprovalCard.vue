<script setup>
/* ============================================================
   ClickApprovalCard —— 「批准并执行 / 拒绝」卡片（阶段 C ②）
   ------------------------------------------------------------
   替代原来的「确认点击」按钮。两处关键差异：

   ★ 为什么是 approval_id 而不是一段文本
     旧实现是「把后端给的确认文本原样再发一遍」，那条路在 2026-10-15
     被封死了 —— 只要在聊天框里手打同样一句话就能触发真实点击，
     审批闸门等于不存在。现在卡片持有的是数据库里一条审批记录的 id：
     批准与执行都经过 POST /api/approvals/{id}/... ，各只能成功一次。

   ★ 为什么倒计时只是「提示」而不是判据
     expires_at 来自服务器。浏览器时钟可能不准，所以剩余秒数只是给用户的
     预期管理；真正说了算的是服务端 —— 过期后点按钮会收到 409/410，
     卡片会把那句话如实显示出来。倒计时归零只是让界面**先**一步放弃，
     避免用户对着一个必然失败的按钮点。

   ★ 为什么「执行失败」是终态，而「批准失败」可以重试
     批准失败时能确定什么都没发生（那条记录要么还是待批准，要么已被处理）；
     而执行请求失败时无法排除「其实已经点过了」—— 允许重试就是允许点两次。
     这与后端把传输层异常记为 failed（不退回 approved）是同一条取舍。
   ============================================================ */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { api } from '../lib/api.js';

const props = defineProps({
  approval: { type: Object, required: true },
});

const emit = defineEmits(['settle']);

const running = ref(false);
const error = ref('');
const now = ref(Date.now());
let timer = null;

const settled = computed(() => Boolean(props.approval.settled));
const outcome = computed(() => props.approval.outcome || '');

/** 服务器给的过期时刻；解析不出来就不显示倒计时（服务端仍会兜住）。 */
const deadline = computed(() => {
  const parsed = Date.parse(props.approval.expires_at || '');
  return Number.isFinite(parsed) ? parsed : null;
});

const remaining = computed(() =>
  deadline.value === null ? null : Math.max(0, Math.ceil((deadline.value - now.value) / 1000)));

const expired = computed(() => remaining.value !== null && remaining.value <= 0);

/** 落一个终态。幂等 —— 定时器与用户点击可能同时触发。 */
function settle(state, message) {
  if (settled.value) return;
  emit('settle', { outcome: state, message });
}

function tick() {
  now.value = Date.now();
  // 超时由**界面**先放弃：让按钮变成不可点，比让用户点了再收 410 更清楚。
  // 服务端那条记录会在下次读列表时被惰性置为 expired。
  if (!settled.value && !running.value && expired.value) {
    settle('expired', '已超时自动放弃，没有执行任何点击。');
  }
}

onMounted(() => {
  timer = setInterval(tick, 1000);
});
onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
});

async function approveAndExecute() {
  if (running.value || settled.value || expired.value) return;
  running.value = true;
  error.value = '';

  try {
    await api.approveApproval(props.approval.approval_id);
  } catch (e) {
    // 批准这一步失败 ⇒ 确定什么都没发生，按钮留着让用户重试
    running.value = false;
    error.value = `未能批准：${e.message}`;
    return;
  }

  try {
    const result = await api.executeApproval(props.approval.approval_id);
    running.value = false;
    if (result.status === 'executed') {
      // ★ 不写「页面标题不可用」：点击常常伴随跳转，此时浏览器读到的 title
      //   经常还是空的。一句「不可用」会让一次**成功**的点击读起来像失败。
      //   标题缺失就退到路径 —— 路径来自 urlparse，跳转后一定有值。
      const page = result.page || {};
      const where = page.title
        ? `当前页面：${page.title}`
        : page.path
          ? `已跳转到：${page.path}`
          : '';
      settle('executed', `已按你的批准点击该控件。${where}`.trim());
    } else if (result.status === 'expired') {
      settle('expired', '这次点击已经作废（页面上的待点击项已消失），什么都没有执行。');
    } else {
      settle('failed', '页面控件已变化或点击失败，未能确认结果；请到网站上核对。');
    }
  } catch (e) {
    // ★ 终态：无法区分「没送达」与「送达了但没等到回复」，后者可能已经点过了
    running.value = false;
    settle('failed', `执行结果未能确认：${e.message}。请先到网站上核对，再决定是否重新发起。`);
  }
}

async function reject() {
  if (running.value || settled.value) return;
  running.value = true;
  error.value = '';
  try {
    await api.rejectApproval(props.approval.approval_id);
    running.value = false;
    settle('rejected', '已拒绝，没有执行任何点击。');
  } catch (e) {
    running.value = false;
    error.value = `未能拒绝：${e.message}`;
  }
}
</script>

<template>
  <div class="msg-approval" :class="`approval-${outcome || 'pending'}`">
    <div class="approval-head">
      <span class="approval-label">等待批准的一次点击</span>
      <span v-if="!settled && remaining !== null" class="approval-timer">
        {{ remaining }} 秒后自动放弃
      </span>
    </div>

    <p class="approval-target">{{ approval.target }}</p>

    <p v-if="settled" class="approval-result">{{ approval.message }}</p>

    <template v-else>
      <div class="approval-actions">
        <button
          type="button"
          class="btn-confirm"
          :disabled="running || expired"
          @click="approveAndExecute"
        >{{ running ? '执行中…' : '批准并执行' }}</button>
        <button
          type="button"
          class="btn-ghost"
          :disabled="running || expired"
          @click="reject"
        >拒绝</button>
      </div>
      <p class="confirm-hint">只执行这一次；过期或已执行即作废，不会自动重试。</p>
      <p v-if="error" class="approval-error">{{ error }}</p>
    </template>
  </div>
</template>
