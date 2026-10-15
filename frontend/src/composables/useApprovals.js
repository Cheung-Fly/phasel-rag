/* ============================================================
   useApprovals —— 「待批准操作」侧栏面板（阶段 C ② 的界面收口）
   ------------------------------------------------------------
   为什么审批列表需要一个独立的侧栏入口：
     审批卡片挂在**消息**上，只有翻到那条消息才看得到。但「等待批准」
     这件事是有时效的（90 秒），用户可能在别的会话里、或已经滚远了，
     那条待批准的点击/命令就被淹没在聊天流里，直到过期都不被发现。
     侧栏面板给「有哪些操作正在等我点头」一个稳定的、不随滚动消失的入口。

   ★ 批准/执行/拒绝在这里**与消息卡片共用同一套语义**：
     - approve + execute 是两次请求（各只能成功一次），与 ClickApprovalCard 一致；
     - 执行成功后要回显 stdout（终端命令）或跳转结果（网页点击）。
   所以这里的 approveAndExecute 与卡片里的逻辑刻意对齐，只是 UI 载体不同。

   ★ 为什么刷新由用户触发，不自动轮询：
     与审计时间线同因 —— 这是「事实清单」，不是进度条。轮询会让
     「刚才那条还在吗」变成一个随时在变的答案。要看最新，点刷新。
   ============================================================ */
import { ref } from 'vue';
import { api } from '../lib/api.js';

const approvals = ref([]);
const loading = ref(false);
const error = ref('');
const loaded = ref(false);
const acting = ref('');   // 正在处理的 approval_id，用于按钮禁用态

// 侧栏只预览这么多条；其余进浮层。
const PREVIEW_COUNT = 3;
const PAGE_SIZE = 20;

export function useApprovals() {
  /** 拉取审批列表。失败时**不清空**：一次抖动就让「待批准项」消失，
      比显示一份略旧但真实的清单危险得多。 */
  async function refresh() {
    if (loading.value) return;
    loading.value = true;
    error.value = '';
    try {
      const data = await api.listApprovals(PAGE_SIZE);
      approvals.value = data.approvals || [];
      loaded.value = true;
    } catch (err) {
      error.value = err.message || '读取失败';
    } finally {
      loading.value = false;
    }
  }

  /** 批准并执行一条审批（与 ClickApprovalCard.approveAndExecute 同语义）。 */
  async function approveAndExecute(approvalId) {
    if (acting.value) return;
    acting.value = approvalId;
    error.value = '';
    try {
      await api.approveApproval(approvalId);
      await api.executeApproval(approvalId);
      // 执行完刷新列表，让面板里的状态与后端一致。
      await refresh();
    } catch (err) {
      // 批准失败 vs 执行失败要分开提示：前者可重试，后者是终态、不能重试。
      error.value = err.message || '操作失败';
    } finally {
      acting.value = '';
    }
  }

  /** 拒绝一条审批。 */
  async function reject(approvalId) {
    if (acting.value) return;
    acting.value = approvalId;
    error.value = '';
    try {
      await api.rejectApproval(approvalId);
      await refresh();
    } catch (err) {
      error.value = err.message || '拒绝失败';
    } finally {
      acting.value = '';
    }
  }

  return {
    approvals,
    loading,
    error,
    loaded,
    acting,
    preview: PREVIEW_COUNT,
    refresh,
    approveAndExecute,
    reject,
  };
}
