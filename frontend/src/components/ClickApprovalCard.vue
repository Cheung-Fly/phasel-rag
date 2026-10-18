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

/* ★ 终端执行（run_shell）与网页点击（click_element）是两种审批。
   终端审批展示的是「命令 + 人话翻译」，执行后要回显 stdout/exit_code；
   点击审批展示的是「目标控件」，执行后要回显跳转结果。
   数据分析（data_analysis）是第三种：展示「分析目标 + 脚本摘要」，
   执行后回显统计结论（stdout），与终端执行同构。 */
const isShell = computed(() => props.approval.action === 'run_shell');
const isAnalysis = computed(() => props.approval.action === 'data_analysis');
const isMcp = computed(() => props.approval.action === 'mcp_tool');
const isOutputAction = computed(() => isShell.value || isAnalysis.value || isMcp.value);

/** 后端生成的「人话翻译」：用户不用懂 shell 也能判断这条命令在做什么。 */
const shellSummary = computed(() => {
  const d = props.approval.detail || {};
  return typeof d.summary === 'string' && d.summary ? d.summary : '';
});

/** 执行后回显的命令输出（stdout，已由后端截断）。 */
const shellStdout = computed(() => {
  const d = props.approval.detail || {};
  return typeof d.stdout === 'string' ? d.stdout : '';
});

/** 执行后回显的退出码。注意 0 是合法的成功退出码，不能当「没有」处理。 */
const shellExitCode = computed(() => {
  const d = props.approval.detail || {};
  return typeof d.exit_code === 'number' ? d.exit_code : null;
});

/** 数据分析生成的图表文件名列表（回传后渲染 <img>）。 */
const artifacts = computed(() => {
  const d = props.approval.detail || {};
  return Array.isArray(d.artifacts) ? d.artifacts : [];
});

/** 图表文件的访问 URL（经 nginx 反代到 /api/artifacts/{file}）。 */
function artifactUrl(name) {
  return `/api/artifacts/${encodeURIComponent(name)}`;
}

/** 服务器给的过期时刻；解析不出来就不显示倒计时（服务端仍会兜住）。 */
const deadline = computed(() => {
  const parsed = Date.parse(props.approval.expires_at || '');
  return Number.isFinite(parsed) ? parsed : null;
});

const remaining = computed(() =>
  deadline.value === null ? null : Math.max(0, Math.ceil((deadline.value - now.value) / 1000)));

const expired = computed(() => remaining.value !== null && remaining.value <= 0);

/** 落一个终态。幂等 —— 定时器与用户点击可能同时触发。 */
function settle(state, message, extra = {}) {
  if (settled.value) return;
  emit('settle', { outcome: state, message, ...extra });
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
      if (isOutputAction.value) {
        // ★ 终端命令 / 数据分析脚本执行成功：把 stdout/exit_code 写回审批对象，
        //   让卡片立即回显「命令/脚本输出了什么」。这正是 Q5「结果回显在卡片自身」的落点。
        const detail = { ...(props.approval.detail || {}) };
        if (result.stdout !== undefined) detail.stdout = result.stdout;
        if (isMcp.value && typeof result.result === 'string') detail.stdout = result.result;
        if (result.exit_code !== undefined) detail.exit_code = result.exit_code;
        // ★ 数据分析图表回传：把本次生成的图表文件名写回 detail.artifacts，
        //   卡片据此渲染 <img>，让用户真正「看到」分析出的图（闭环的最后一环）。
        if (Array.isArray(result.artifacts)) detail.artifacts = result.artifacts;
        emit('settle', {
          outcome: 'executed',
          message: isAnalysis.value ? '分析已完成。' : '已执行。',
          detail,
        });
      } else {
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
      }
    } else if (result.status === 'expired') {
      settle('expired', '这次点击已经作废（页面上的待点击项已消失），什么都没有执行。');
    } else if (result.status === 'failed') {
      // 终端命令/分析脚本执行失败：后端把原因放在 reason 里（forbidden/timeout/...），
      // 如实告诉用户这条命令为什么没成。
      settle('failed', isOutputAction.value
        ? `命令未能执行${result.reason ? `（${result.reason}）` : ''}。`
        : '页面控件已变化或点击失败，未能确认结果；请到网站上核对。');
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
      <span class="approval-label">
        {{ isMcp ? '等待批准的一次外部工具调用' : isAnalysis ? '等待批准的一次数据分析' : (isShell ? '等待批准的一条终端命令' : '等待批准的一次点击') }}
      </span>
      <span v-if="!settled && remaining !== null" class="approval-timer">
        {{ remaining }} 秒后自动放弃
      </span>
    </div>

    <!-- 终端命令：命令文本 + 后端生成的人话翻译（用户不用懂 shell 也能判断） -->
    <template v-if="isShell || isMcp">
      <p class="approval-target"><code class="approval-cmd">{{ approval.target }}</code></p>
      <p v-if="shellSummary" class="approval-summary">{{ shellSummary }}</p>
    </template>
    <!-- 数据分析：分析目标 + 脚本在做什么（人话摘要） -->
    <template v-else-if="isAnalysis">
      <p class="approval-target">{{ approval.target }}</p>
      <p v-if="shellSummary" class="approval-summary">脚本将：{{ shellSummary }}</p>
    </template>
    <!-- 网页点击：目标控件文本 -->
    <template v-else>
      <p class="approval-target">{{ approval.target }}</p>
    </template>

    <p v-if="settled" class="approval-result">{{ approval.message }}</p>

    <!-- 终端命令 / 分析脚本执行成功后，回显 stdout/exit_code（Q5：结果回显在卡片自身） -->
    <div v-if="settled && isOutputAction && outcome === 'executed' && (shellStdout || shellExitCode !== null)" class="approval-output">
      <div class="approval-output-head">
        <span>{{ isAnalysis ? '分析结果' : isMcp ? '工具返回' : '命令输出' }}</span>
        <span v-if="shellExitCode !== null" class="approval-exit" :class="shellExitCode === 0 ? 'ok' : 'err'">
          退出码 {{ shellExitCode }}
        </span>
      </div>
      <pre v-if="shellStdout" class="approval-stdout">{{ shellStdout }}</pre>
      <p v-else class="approval-output-empty">（无输出）</p>
    </div>

    <!-- 数据分析执行成功后回传的图表（闭环最后一环：让用户真正看到图） -->
    <div v-if="settled && isAnalysis && outcome === 'executed' && artifacts.length" class="approval-artifacts">
      <figure v-for="name in artifacts" :key="name" class="approval-figure">
        <img :src="artifactUrl(name)" :alt="name" loading="lazy" class="approval-chart" />
        <figcaption class="approval-figcaption">{{ name }}</figcaption>
      </figure>
    </div>

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
