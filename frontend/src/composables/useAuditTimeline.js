/* ============================================================
   useAuditTimeline —— 「助手做过什么」的证据页（阶段 C ④）
   ------------------------------------------------------------
   为什么状态放在共享 composable：
     侧栏面板（只显示最近几条）与浮层（完整流水 + 翻页）读的是同一份数据。
     两处各持一份 ref 的话，「浮层里刷新了、侧栏还是旧的」几乎必然发生 ——
     而这页存在的意义就是「能核对刚刚发生了什么」，两份不一致直接毁掉它。
     与 useSites / useDocuments 的取舍一致。

   ★ 这里**不重新排序、不去重、不自己插新记录**。
     顺序由后端 SQL 决定（跨两张表的全局倒序），游标也由后端给。
     前端若自己往列表头上插一条「刚刚的操作」，它立刻和游标分页打架 ——
     下一次「加载更多」会从某个旧位置续上，中间那条被跳过或重复。
     所以刷新是唯一的真相来源，新增记录靠用户主动刷新看到。

   形态取舍：翻页用「加载更多」按钮而不是无限滚动。
     浮层高度有限、又没有地址栏反馈，无限滚动很容易出现
     「以为到底了、其实还有」。一个写着「加载更多」的按钮不会骗人。
   ============================================================ */
import { ref } from 'vue';
import { api } from '../lib/api.js';

// 侧栏面板只预览这么多条；完整列表在浮层里。
const PREVIEW_COUNT = 3;
// 一页取多少条。上限由后端定（≤100），这里不越权猜。
const PAGE_SIZE = 30;

const events = ref([]);
const loading = ref(false);
const error = ref('');
const hasMore = ref(false);
// 下一页的锚点。它是后端给的**排序键**，不是偏移量，前端只负责原样回传。
const cursor = ref('');
// 是否已经取过第一页。区分「还没取」与「取到了空」 ——
// 两者在前端都是空数组，但文案完全不同（「加载中」vs「还没有记录」）。
const loaded = ref(false);
// 浮层开关。放在这里而不是组件内部：侧栏按钮要打开它，
// 而按钮和浮层在两个组件里（浮层 Teleport 到 body）。
const drawerOpen = ref(false);

export function useAuditTimeline() {
  /** 取第一页，整体替换。失败时保留已有内容，只把错误显示出来。 */
  async function refresh() {
    if (loading.value) return;
    loading.value = true;
    error.value = '';
    try {
      const data = await api.auditTimeline({ limit: PAGE_SIZE });
      events.value = data.events || [];
      hasMore.value = Boolean(data.has_more);
      cursor.value = data.next_cursor || '';
      loaded.value = true;
    } catch (err) {
      // ★ 不清空 events：一次网络抖动就让「助手做过什么」变成一张白纸，
      //   比保留一份略旧的流水坏得多 —— 用户会以为记录被抹掉了。
      error.value = err.message || '读取失败';
    } finally {
      loading.value = false;
    }
  }

  /** 取下一页并追加。 */
  async function loadMore() {
    if (loading.value || !hasMore.value || !cursor.value) return;
    loading.value = true;
    error.value = '';
    try {
      const data = await api.auditTimeline({
        limit: PAGE_SIZE,
        cursor: cursor.value,
      });
      events.value = [...events.value, ...(data.events || [])];
      hasMore.value = Boolean(data.has_more);
      cursor.value = data.next_cursor || '';
    } catch (err) {
      error.value = err.message || '读取失败';
    } finally {
      loading.value = false;
    }
  }

  function openDrawer() {
    drawerOpen.value = true;
    // 浮层里的「刷新」和侧栏是同一个动作，不必各写一份。
    if (!loaded.value) refresh();
  }

  function closeDrawer() {
    drawerOpen.value = false;
  }

  return {
    events,
    loading,
    error,
    hasMore,
    loaded,
    drawerOpen,
    preview: PREVIEW_COUNT,
    refresh,
    loadMore,
    openDrawer,
    closeDrawer,
  };
}
