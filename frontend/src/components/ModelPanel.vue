<script setup>
import { onMounted, ref } from 'vue';
import { api } from '../lib/api.js';
import { useModels } from '../composables/useModels.js';
import ModalDialog from './ModalDialog.vue';

const { profiles, activeLabel, error, load } = useModels();
const open = ref(false);
const message = ref('');
const failed = ref(false);
const form = ref({ name: '', model: '', base_url: '', api_key: '' });

async function run(action, success) {
  message.value = '';
  failed.value = false;
  try {
    await action();
    message.value = success;
  } catch (exception) {
    failed.value = true;
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
    <div class="model-row">
      <h2 class="panel-title">模型</h2>
      <span class="model-current">{{ activeLabel }}</span>
      <button type="button" class="btn-ghost" @click="open = true">管理</button>
    </div>
  </section>

  <ModalDialog v-if="open" title="管理模型" @close="open = false">
    <h4>已添加的模型</h4>
    <ul v-if="profiles.length" class="modal-list">
      <li v-for="profile in profiles" :key="profile.profile_id">
        <span>{{ profile.name }} · {{ profile.model }}{{ profile.active ? '（使用中）' : '' }}</span>
        <button type="button" class="link-button" @click="run(() => api.testModel(profile.profile_id), '连接正常')">测试</button>
        <button type="button" class="link-button" @click="run(() => api.deleteModel(profile.profile_id), '已删除')">删除</button>
      </li>
    </ul>
    <p v-else class="modal-msg">还没有添加模型，当前使用默认模型。</p>
    <h4>添加模型</h4>
    <form class="modal-form" @submit.prevent="save">
      <input v-model="form.name" required maxlength="80" placeholder="档案名称" />
      <input v-model="form.model" required placeholder="模型名，如 deepseek-chat" />
      <input v-model="form.base_url" required type="url" placeholder="https://api.example.com/v1" />
      <input v-model="form.api_key" required type="password" autocomplete="off" placeholder="API Key（加密保存）" />
      <button type="submit" class="btn-ghost">添加模型</button>
    </form>
    <p v-if="message || error" class="modal-msg" :class="{ err: failed || error }">{{ message || error }}</p>
  </ModalDialog>
</template>

<style scoped>
.model-row { display: flex; align-items: center; gap: 8px; }
.model-current { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; color: var(--text-faint); }
</style>
