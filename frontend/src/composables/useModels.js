import { computed, ref } from 'vue';
import { api } from '../lib/api.js';

const profiles = ref([]);
const defaultModel = ref('');
const switching = ref(false);
const error = ref('');

// 当前选择：'' 表示环境变量里的默认模型，否则是档案 ID。
const activeId = computed(() => profiles.value.find((p) => p.active)?.profile_id || '');
const activeLabel = computed(() => profiles.value.find((p) => p.active)?.model || defaultModel.value || '默认模型');

async function load() {
  try {
    const data = await api.models();
    profiles.value = data.profiles || [];
    defaultModel.value = data.default_model || '';
    error.value = '';
  } catch (exception) {
    error.value = exception.message || '模型配置读取失败';
  }
}

async function select(id) {
  if (switching.value || id === activeId.value) return;
  switching.value = true;
  try {
    await (id ? api.activateModel(id) : api.resetModel());
    error.value = '';
  } catch (exception) {
    error.value = exception.message || '切换模型失败';
  } finally {
    switching.value = false;
    await load();
  }
}

export function useModels() {
  return { profiles, defaultModel, activeId, activeLabel, switching, error, load, select };
}
