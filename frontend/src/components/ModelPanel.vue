<script setup>
import { onMounted, ref } from 'vue';
import { api } from '../lib/api.js';
import { useModels } from '../composables/useModels.js';

const { profiles, activeLabel, error, load } = useModels();
const open = ref(false);
const message = ref('');
const form = ref({ name: '', model: '', base_url: '', api_key: '' });

async function run(action, success) {
  message.value = '';
  try {
    await action();
    message.value = success;
  } catch (exception) {
    message.value = exception.message || '操作失败';
  }
  await load();
}

function save() {
  return run(async () => {
    await api.saveModel(form.value);
    form.value = { name: '', model: '', base_url: '', api_key: '' };
  }, '模型已保存，可在输入框旁切换');
}

onMounted(load);
</script>

<template>
  <section class="panel model-panel">
    <button type="button" class="model-toggle" :aria-expanded="open" @click="open = !open">
      <span class="panel-title">模型</span>
      <span class="model-current">{{ activeLabel }}</span>
      <span class="model-caret">{{ open ? '▾' : '▸' }}</span>
    </button>
    <template v-if="open">
      <ul v-if="profiles.length" class="model-list">
        <li v-for="profile in profiles" :key="profile.profile_id">
          <span>{{ profile.name }} · {{ profile.model }}{{ profile.active ? '（使用中）' : '' }}</span>
          <button type="button" class="link-button" @click="run(() => api.testModel(profile.profile_id), '连接正常')">测试</button>
          <button type="button" class="link-button" @click="run(() => api.deleteModel(profile.profile_id), '已删除')">删除</button>
        </li>
      </ul>
      <form class="model-form" @submit.prevent="save">
        <input v-model="form.name" required maxlength="80" placeholder="档案名称" />
        <input v-model="form.model" required placeholder="模型名，如 deepseek-chat" />
        <input v-model="form.base_url" required type="url" placeholder="https://api.example.com/v1" />
        <input v-model="form.api_key" required type="password" autocomplete="off" placeholder="API Key（加密保存）" />
        <button type="submit" class="btn-ghost">添加模型</button>
      </form>
      <p v-if="message || error" class="model-hint">{{ message || error }}</p>
    </template>
  </section>
</template>

<style scoped>
.model-panel { gap: 6px; }
.model-toggle { display: flex; align-items: center; gap: 8px; width: 100%; border: 0; background: transparent; color: inherit; cursor: pointer; padding: 0; text-align: left; }
.model-current { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; color: var(--text-faint); }
.model-caret { color: var(--text-faint); font-size: 11px; }
.model-hint { margin: 0; font-size: 11px; color: var(--text-faint); }
.model-list { margin: 0; padding-left: 18px; color: var(--text-dim); font-size: 11px; }
.model-form { display: grid; gap: 5px; }
.model-form input { min-width: 0; border: 1px solid var(--border); border-radius: 5px; padding: 5px; background: var(--surface); color: var(--text); font-size: 11px; }
.link-button { border: 0; background: transparent; color: var(--accent); cursor: pointer; font-size: 11px; padding: 0 3px; }
</style>
