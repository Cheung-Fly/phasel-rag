import { ref } from 'vue';
import { api } from '../lib/api.js';

const snapshot = ref({ skills: [], mcp_servers: [] });
const loading = ref(false);
const error = ref('');

export function useExtensions() {
  async function refresh() {
    loading.value = true;
    error.value = '';
    try {
      snapshot.value = await api.extensions();
    } catch (exception) {
      error.value = exception.message || '扩展状态读取失败';
    } finally {
      loading.value = false;
    }
  }

  return { snapshot, loading, error, refresh };
}
