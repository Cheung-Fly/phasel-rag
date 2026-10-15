/* ============================================================
   useSuggestions —— 主动建议的拉取 / 采纳 / 忽略
   ------------------------------------------------------------
   ★ 为什么是「入口触发」而不是后台定时
     本机是「网页界面 + 单人」的形态，没有手机推送通道。建议只有在
     用户「在」的时候送达才有意义，所以拉取发生在用户打开页面时。

   ★ 为什么采纳后要「乐观移除」这条建议
     采纳 = pending → accepted，且会转成一个后台任务。这条建议从
     待决定列表里消失是确定的事（后端已落库），本地直接移除即可，
     不必等一次刷新。忽略同理。这避免了「点了采纳，卡片还赖在那」。
   ============================================================ */
import { ref } from 'vue';
import { api } from '../lib/api.js';
import { useToast } from './useToast.js';

const suggestions = ref([]);
const loading = ref(false);
const error = ref('');
const loaded = ref(false);
const acting = ref(''); // 正在处理（采纳/忽略）的建议 id，避免重复点击

async function refresh() {
  loading.value = true;
  try {
    const data = await api.listSuggestions();
    suggestions.value = data.suggestions || [];
    error.value = '';
  } catch (e) {
    error.value = e.message;
  } finally {
    loaded.value = true;
    loading.value = false;
  }
}

/** 采纳：转后台任务。成功后从本地列表移除，并刷新任务面板由调用方触发。 */
async function accept(id) {
  const { toast } = useToast();
  if (acting.value) return;
  acting.value = id;
  try {
    await api.acceptSuggestion(id);
    suggestions.value = suggestions.value.filter((s) => s.id !== id);
    toast('已采纳，转成后台任务执行');
  } catch (e) {
    toast('采纳失败：' + e.message, 'error');
  } finally {
    acting.value = '';
  }
}

/** 忽略：冷却期内不再推送。成功后从本地列表移除。 */
async function dismiss(id) {
  const { toast } = useToast();
  if (acting.value) return;
  acting.value = id;
  try {
    await api.dismissSuggestion(id);
    suggestions.value = suggestions.value.filter((s) => s.id !== id);
    toast('已忽略，7 天内不再提示同类建议');
  } catch (e) {
    toast('操作失败：' + e.message, 'error');
  } finally {
    acting.value = '';
  }
}

export function useSuggestions() {
  return { suggestions, loading, error, loaded, acting, refresh, accept, dismiss };
}
