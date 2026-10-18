<script setup>
import { onMounted, ref } from 'vue';
import { api } from '../lib/api.js';

const profiles = ref([]);
const defaultModel = ref('');
const message = ref('');
const form = ref({ name: '', model: '', base_url: '', api_key: '' });

async function load() {
  try {
    const data = await api.models();
    profiles.value = data.profiles || [];
    defaultModel.value = data.default_model || '';
  } catch (exception) {
    message.value = exception.message || '模型配置读取失败';
  }
}

async function run(action, success) {
  message.value = '';
  try {
    await action();
    message.value = success;
    await load();
  } catch (exception) {
    message.value = exception.message || '操作失败';
  }
}

function save() {
  return run(async () => {
    await api.saveModel(form.value);
    form.value = { name: '', model: '', base_url: '', api_key: '' };
  }, '模型已保存');
}

onMounted(load);
</script>

<template>
  <section class="panel model-panel">
    <h2 class="panel-title">模型</h2>
    <p class="model-hint">
      当前：{{ profiles.find((p) => p.active)?.model || `${defaultModel}（默认）` }}
    </p>
    <ul v-if="profiles.length" class="model-list">
      <li v-for="profile in profiles" :key="profile.profile_id">
        <span>{{ profile.name }} · {{ profile.model }}{{ profile.active ? '（使用中）' : '' }}</span>
        <button type="button" class="link-button" @click="run(() => api.activateModel(profile.profile_id), '已切换模型')">启用</button>
        <button type="button" class="link-button" @click="run(() => api.testModel(profile.profile_id), '连接正常')">测试</button>
        <button type="button" class="link-button" @click="run(() => api.deleteModel(profile.profile_id), '已删除')">删除</button>
      </li>
    </ul>
    <button v-if="profiles.some((p) => p.active)" type="button" class="btn-ghost" @click="run(api.resetModel, '已恢复默认模型')">
      恢复默认模型
    </button>
    <form class="model-form" @submit.prevent="save">
      <input v-model="form.name" required maxlength="80" placeholder="档案名称" />
      <input v-model="form.model" required placeholder="模型名，如 deepseek-chat" />
      <input v-model="form.base_url" required type="url" placeholder="https://api.example.com/v1" />
      <input v-model="form.api_key" required type="password" autocomplete="off" placeholder="API Key（加密保存）" />
      <button type="submit" class="btn-ghost">保存模型</button>
    </form>
    <p v-if="message" class="model-hint">{{ message }}</p>
  </section>
</template>

<style scoped>
.model-panel { gap: 6px; }
.model-hint { margin: 0; font-size: 11px; color: var(--text-faint); }
.model-list { margin: 0; padding-left: 18px; color: var(--text-dim); font-size: 11px; }
.model-form { display: grid; gap: 5px; }
.model-form input { min-width: 0; border: 1px solid var(--border); border-radius: 5px; padding: 5px; background: var(--surface); color: var(--text); font-size: 11px; }
.link-button { border: 0; background: transparent; color: var(--accent); cursor: pointer; font-size: 11px; padding: 0 3px; }
</style>
