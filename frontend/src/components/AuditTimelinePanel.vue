<script setup>
/* ============================================================
   AuditTimelinePanel —— 「助手做过什么」（阶段 C ④）
   ------------------------------------------------------------
   这是阶段 C「信任面」的收口：前三个 Wave 分别是「助手能碰哪些站点」
   （W2）、「碰之前要不要你点头」（W3），而这一页回答的是
   **「它到底碰过什么」**。没有它，前两个 Wave 只能靠信任，不能靠核对。

   形态取舍：侧栏只放最近几条 + 一个「查看全部」，完整流水另开浮层。
     理由与 SitePanel 一致 —— 316px 宽的侧栏放不下一份要按天读的流水，
     而它是「翻旧账」时才看的东西，适合浮层。反过来，最近几条留在侧栏
     是有用的：它让「刚刚那次批准真的执行了吗」不必点开就能看到。

   ★ 重新加载由用户触发（刷新按钮），不自动轮询。
     审计是**事实记录**，不是进度条 —— 每 5 秒变一次的数字只会让人
     怀疑自己刚才看的是哪一版。要看最新的，点刷新。

   ★ 浮层必须 Teleport 到 body：窄屏时侧栏带 transform，而 position:fixed
     的元素若位于 transform 祖先内部，会以那个祖先为包含块 —— 表现就是
     浮层被裁进侧栏里。这条坑 SitePanel 已经踩过一次。
   ============================================================ */
import { useAuditTimeline } from '../composables/useAuditTimeline.js';

const {
  events,
  loading,
  error,
  hasMore,
  loaded,
  drawerOpen,
  preview,
  refresh,
  loadMore,
  openDrawer,
  closeDrawer,
} = useAuditTimeline();

/**
 * 时间戳。今天的只显示时分秒，其余带上月日 —— 与 TaskPanel 的判断一致。
 * 秒保留下来是有意的：这页是用来核对的，同一分钟内发生两件事（批准 → 执行）
 * 是常态，只显示到分钟就分不出先后了。
 */
function timeLabel(iso) {
  if (!iso) return '';
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return iso;
  const time = when.toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  return when.toDateString() === new Date().toDateString()
    ? time
    : `${when.toLocaleDateString('zh-CN')} ${time}`;
}

// level 由后端给（见 audit_timeline.py 的 _LEVELS）。前端不猜语义，
// 只把四个取值映射到颜色；遇到没见过的值一律当 muted，不崩。
const LEVELS = ['ok', 'warn', 'info', 'muted'];
function levelClass(event) {
  return LEVELS.includes(event.level) ? `lvl-${event.level}` : 'lvl-muted';
}
</script>

<template>
  <section class="panel">
    <h3 class="panel-title">
      助手做过什么
      <span class="count">{{ events.length }}</span>
      <button class="link-btn" type="button" @click="openDrawer">查看全部</button>
    </h3>

    <ul class="audit-list">
      <!-- 顺序有意义：先报错、再「还没取到」、再空、最后才是列表。
           「还没取到」与「取到了空」在数据上都是空数组，但文案必须分开 ——
           把加载中说成「还没有记录」会让人以为记录丢了。 -->
      <li v-if="error" class="empty">读取失败：{{ error }}</li>
      <li v-else-if="!loaded" class="empty">加载中…</li>
      <li v-else-if="!events.length" class="empty">还没有记录</li>
      <template v-else>
        <li
          v-for="event in events.slice(0, preview)"
          :key="event.id"
          :class="levelClass(event)"
        >
          <span class="audit-time">{{ timeLabel(event.created_at) }}</span>
          <span class="audit-text" :title="event.summary">{{ event.summary }}</span>
        </li>
      </template>
    </ul>

    <p class="audit-hint">
      登记/查看凭证、以及需要你批准的操作，都会记在这里。
    </p>
  </section>

  <!-- Teleport 到 body，理由见本文件头部注释 -->
  <Teleport to="body">
    <div v-if="drawerOpen" class="audit-backdrop" @click.self="closeDrawer">
      <section class="audit-modal" role="dialog" aria-modal="true">
        <header>
          <h3>助手做过什么</h3>
          <div class="audit-actions">
            <button type="button" class="btn-ghost" :disabled="loading" @click="refresh">
              {{ loading ? '读取中…' : '刷新' }}
            </button>
            <button type="button" class="btn-ghost" @click="closeDrawer">关闭</button>
          </div>
        </header>

        <p class="audit-note">
          按时间倒序。已加载 {{ events.length }} 条<span v-if="hasMore">，更早的还没取</span>。
        </p>

        <ol class="audit-full">
          <li v-if="!loaded && !error" class="empty">加载中…</li>
          <li v-else-if="!events.length && !error" class="empty">还没有记录</li>
          <li v-for="event in events" :key="event.id" :class="levelClass(event)">
            <span class="audit-time">{{ timeLabel(event.created_at) }}</span>
            <span class="audit-text">{{ event.summary }}</span>
          </li>
        </ol>

        <p v-if="error" class="audit-error" role="alert">{{ error }}</p>

        <footer>
          <button v-if="hasMore" type="button" :disabled="loading" @click="loadMore">
            {{ loading ? '加载中…' : '加载更多' }}
          </button>
          <!-- 「没有更多了」只在确实取过、且确实还有内容时显示，
               否则一打开就是一句「没有更多了」叠在「还没有记录」下面。 -->
          <span v-else-if="loaded && events.length" class="audit-end">没有更多了</span>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.audit-list { list-style: none; margin: 0; padding: 0; }
.audit-list li, .audit-full li {
  display: flex; align-items: baseline; gap: 7px;
  padding: 5px 8px; border-radius: var(--radius-sm);
  font-size: 12px; background: var(--bg-input); margin-bottom: 4px;
}
.audit-list li.empty, .audit-full li.empty {
  background: none; color: var(--text-faint);
  font-size: 11.5px; justify-content: center;
}
/* 左侧那道竖线是这页唯一的状态信号。颜色语义与审批卡片保持一致：
   ok=真的做成了，warn=需要你留意，info=普通事件，muted=只是看了一眼。 */
.audit-list li, .audit-full li { border-left: 2px solid transparent; }
.audit-list li.lvl-ok, .audit-full li.lvl-ok { border-left-color: var(--ok); }
.audit-list li.lvl-warn, .audit-full li.lvl-warn { border-left-color: var(--warn); }
.audit-list li.lvl-info, .audit-full li.lvl-info { border-left-color: var(--accent-dim); }
.audit-list li.lvl-muted, .audit-full li.lvl-muted { border-left-color: var(--border); }

.audit-time {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
  font-size: 10.5px;
  color: var(--text-faint);
}
.audit-text {
  flex: 1; min-width: 0;
  color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.audit-hint { margin: 0; font-size: 10.5px; color: var(--text-faint); }

/* ---------- 浮层 ---------- */
.audit-backdrop {
  position: fixed; inset: 0; z-index: 40;
  display: grid; place-items: center; padding: 18px;
  background: rgb(0 0 0 / 60%);
  overflow: auto;
}
.audit-modal {
  display: grid; gap: 10px;
  grid-template-rows: auto auto 1fr auto auto;
  width: min(640px, 100%);
  max-height: 86vh;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  box-shadow: var(--shadow);
}
.audit-modal header {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
}
.audit-modal h3 { margin: 0; font-size: 15px; }
.audit-actions { display: flex; gap: 8px; }
.audit-note { margin: 0; font-size: 11.5px; color: var(--text-dim); }
.audit-full {
  list-style: none; margin: 0; padding: 0;
  overflow: auto;
  /* 长流水必须能滚。没有这条，grid 的 1fr 会让它把浮层顶出屏幕。 */
  min-height: 60px;
}
/* 浮层里的行允许换行：侧栏可以省略号，但「核对」时被截断的句子毫无用处。 */
.audit-full .audit-text { white-space: normal; }
.audit-full .audit-time { padding-top: 1px; }
.audit-error { margin: 0; color: var(--err); font-size: 12px; }
.audit-modal footer {
  display: flex; align-items: center; justify-content: center;
  min-height: 22px;
}
.audit-modal footer button {
  padding: 6px 16px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-input); color: var(--text-dim);
  cursor: pointer; font-size: 12px;
}
.audit-modal footer button:disabled { opacity: .6; cursor: wait; }
.audit-end { font-size: 11px; color: var(--text-faint); }
.audit-modal header .btn-ghost {
  padding: 5px 10px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-input); color: var(--text-dim);
  cursor: pointer; font-size: 12px;
}
.audit-modal header .btn-ghost:disabled { opacity: .6; cursor: wait; }
</style>
