<script setup>
/* ============================================================
   TaskPanel —— 后台任务面板
   ------------------------------------------------------------
   阶段 B 起，文档入库不再占用 HTTP 请求：上传只负责「提交」，
   真正的解析/切片/向量化在后台执行器里跑。这个面板就是那件事的唯一可见窗口。

   ★ 为什么面板在没有任务时也要显示（只要执行器离线）
     「上传成功但一直排队」是这套异步设计最典型的故障：从界面上看和
     「正在处理」几乎一样。所以只要有任务、或执行器被判定离线，面板就必须在场，
     好把原因说出来；两者都没有时才收起，避免占地方。

   ★ 为什么任务完成时要刷新文档列表
     入库成功的标志是新文档出现在知识库里。让用户自己去点一次刷新
     才能看到成果，是把「实现是否闭环」的验证责任推给了他。
   ============================================================ */
import { computed, onMounted, onUnmounted, watch } from 'vue';
import { useTasks } from '../composables/useTasks.js';
import { useDocuments } from '../composables/useDocuments.js';
import { useHealth } from '../composables/useHealth.js';

const { tasks, listError, activeCount, isActive, start, stop, cancel } = useTasks();
const { refresh: refreshDocs } = useDocuments();
const { workerOffline } = useHealth();

const STATUS_LABEL = {
  queued: '排队中',
  running: '进行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

const visible = computed(() => tasks.value.length > 0 || workerOffline.value);

function taskTitle(task) {
  return task.payload?.filename || task.task_type;
}

function label(task) {
  return STATUS_LABEL[task.status] || task.status;
}

/** 返回 0-100，或 null 表示「进度不可测」（解析/切片阶段没有分母）。 */
function pct(task) {
  const total = task.progress_total || 0;
  if (total <= 0) return null;
  return Math.min(100, Math.round(((task.progress_current || 0) / total) * 100));
}

function timeLabel(task) {
  const iso = task.finished_at || task.started_at || task.created_at;
  if (!iso) return '';
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return '';
  const time = when.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  return when.toDateString() === new Date().toDateString()
    ? time
    : `${when.toLocaleDateString('zh-CN')} ${time}`;
}

function meta(task) {
  if (isActive(task)) {
    const stage = task.stage || '准备中';
    const total = task.progress_total || 0;
    return total > 0 ? `${stage} ${task.progress_current}/${total}` : stage;
  }
  const bits = [];
  if (task.status === 'succeeded' && task.result?.chunks != null) {
    bits.push(`已入库 ${task.result.chunks} 个片段`);
  }
  // attempts 是「被领取次数」，重试次数是它减一。暴露出来是因为
  // 「跑了两遍」如果不显示，用户只会觉得「怎么这么慢」。
  if (task.attempts > 1) bits.push(`重试 ${task.attempts - 1} 次`);
  const when = timeLabel(task);
  if (when) bits.push(when);
  return bits.join(' · ');
}

const finishedCount = computed(() => tasks.value.filter((t) => !isActive(t)).length);

watch(finishedCount, (now, before) => {
  if (now > before) refreshDocs();
});

onMounted(start);
onUnmounted(stop);
</script>

<template>
  <section v-if="visible" class="panel">
    <h3 class="panel-title">
      后台任务
      <span v-if="activeCount" class="count">{{ activeCount }}</span>
    </h3>

    <p v-if="workerOffline" class="task-warn" role="alert">
      后台执行器未运行，任务会一直排队。请确认 rag-worker 容器已启动。
    </p>
    <p v-if="listError" class="task-error">读取失败：{{ listError }}</p>

    <ul class="task-list">
      <li v-for="task in tasks" :key="task.task_id" :class="'st-' + task.status">
        <div class="task-head">
          <span class="task-name" :title="taskTitle(task)">{{ taskTitle(task) }}</span>
          <span class="task-badge">{{ label(task) }}</span>
        </div>

        <div v-if="isActive(task)" class="task-bar">
          <div
            class="task-bar-fill"
            :class="{ indeterminate: pct(task) === null }"
            :style="pct(task) === null ? undefined : { width: pct(task) + '%' }"
          ></div>
        </div>

        <div class="task-foot">
          <span class="task-meta">{{ meta(task) }}</span>
          <!-- 取消有两种结局：排队中会立刻变成「已取消」；运行中只能请求停止，
               所以此时显示「取消中…」而不是假装已经停好了。 -->
          <button
            v-if="isActive(task) && !task.cancel_requested"
            class="task-cancel"
            title="取消这个任务"
            @click="cancel(task.task_id)"
          >取消</button>
          <span v-else-if="isActive(task)" class="task-pending">取消中…</span>
        </div>

        <p v-if="task.status === 'failed' && task.error" class="task-error">{{ task.error }}</p>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.task-warn {
  margin: 0;
  padding: 7px 9px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--warn) 14%, transparent);
  color: var(--warn);
  font-size: 11.5px;
  line-height: 1.45;
}
.task-error { margin: 0; color: var(--err); font-size: 11px; word-break: break-word; }

.task-list { list-style: none; margin: 0; padding: 0; }
.task-list li {
  padding: 7px 8px;
  margin-bottom: 5px;
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  font-size: 12px;
  border-left: 2px solid var(--border);
}
.task-list li.st-running  { border-left-color: var(--accent); }
.task-list li.st-succeeded { border-left-color: var(--ok); }
.task-list li.st-failed   { border-left-color: var(--err); }
.task-list li.st-cancelled { border-left-color: var(--text-faint); }

.task-head { display: flex; align-items: baseline; gap: 6px; }
.task-name {
  flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.task-badge { color: var(--text-dim); font-size: 10.5px; white-space: nowrap; }
.st-succeeded .task-badge { color: var(--ok); }
.st-failed .task-badge { color: var(--err); }

.task-bar {
  height: 4px; margin: 5px 0 4px;
  background: var(--border); border-radius: 2px; overflow: hidden;
}
.task-bar-fill {
  height: 100%; width: 0;
  background: var(--accent); border-radius: 2px;
  transition: width .25s;
}
/* 解析/切片阶段没有分母，用一条来回滑动的条表示「在动但不知道还剩多久」，
   比一个假的百分比诚实。 */
.task-bar-fill.indeterminate {
  width: 35%;
  animation: task-slide 1.2s ease-in-out infinite;
}
@keyframes task-slide {
  0%   { margin-left: 0; }
  50%  { margin-left: 65%; }
  100% { margin-left: 0; }
}

.task-foot { display: flex; align-items: center; gap: 6px; }
.task-meta { flex: 1; min-width: 0; color: var(--text-dim); font-size: 10.5px; }
.task-pending { color: var(--text-faint); font-size: 10.5px; }
.task-cancel {
  border: 1px solid var(--border); background: none;
  color: var(--text-faint); font-size: 10.5px;
  padding: 1px 7px; border-radius: 999px; cursor: pointer;
}
.task-cancel:hover { color: var(--err); border-color: var(--err); }
</style>
