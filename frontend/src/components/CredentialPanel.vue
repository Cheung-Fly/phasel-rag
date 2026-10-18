<script setup>
import { onMounted, ref } from 'vue';
import { api } from '../lib/api.js';
import ModalDialog from './ModalDialog.vue';

const open = ref(false);
const items = ref([]);
const sites = ref([]);
const editingId = ref('');
const message = ref('');
const failed = ref(false);
const blank = () => ({ service_key: '', label: '', site_url: '', full_name: '', student_id: '', account: '', password: '' });
const form = ref(blank());

async function load() {
  const [creds, siteList] = await Promise.all([api.listCredentials(), api.listSites().catch(() => ({ sites: [] }))]);
  items.value = creds.credentials || [];
  sites.value = siteList.sites || [];
}

function pickSite() {
  const site = sites.value.find((item) => item.site_key === form.value.service_key);
  if (!site) return;
  form.value.site_url = site.login_url;
  if (!form.value.label) form.value.label = site.name;
}

async function edit(item) {
  message.value = '';
  try {
    const detail = await api.getCredential(item.credential_id);
    // 口令不回传，编辑时必须重新输入。
    form.value = { ...blank(), ...detail, password: '' };
    editingId.value = item.credential_id;
  } catch (exception) {
    failed.value = true;
    message.value = exception.message || '读取失败';
  }
}

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
  await load().catch(() => {});
}

function save() {
  return run(async () => {
    await api.saveCredential(form.value, editingId.value);
    form.value = blank();
    editingId.value = '';
  }, '已保存');
}

function cancelEdit() {
  form.value = blank();
  editingId.value = '';
}

onMounted(() => load().catch(() => {}));
</script>

<template>
  <section class="panel credential-panel">
    <h2 class="panel-title">个人凭证</h2>
    <div class="credential-row">
      <span>账号 · {{ items.length }}</span>
      <button type="button" class="btn-ghost" @click="open = true">管理</button>
    </div>
  </section>

  <ModalDialog v-if="open" title="管理个人凭证" @close="open = false">
    <h4>已保存的账号</h4>
    <ul v-if="items.length" class="modal-list">
      <li v-for="item in items" :key="item.credential_id">
        <span>{{ item.label }}</span>
        <button type="button" class="link-button" @click="edit(item)">编辑</button>
        <button type="button" class="link-button" @click="run(() => api.deleteCredential(item.credential_id), '已删除')">删除</button>
      </li>
    </ul>
    <p v-else class="modal-msg">还没有保存的账号。</p>
    <h4>{{ editingId ? '修改账号（需重新输入密码）' : '添加账号' }}</h4>
    <form class="modal-form" @submit.prevent="save">
      <input v-model="form.service_key" required maxlength="100" list="credential-sites" placeholder="站点标识（与站点配置一致）" @change="pickSite" />
      <datalist id="credential-sites">
        <option v-for="site in sites" :key="site.site_key" :value="site.site_key">{{ site.name }}</option>
      </datalist>
      <input v-model="form.label" required maxlength="100" placeholder="自定义标签" />
      <input v-model="form.site_url" required type="url" placeholder="登录地址（须与站点配置的登录地址一致）" />
      <input v-model="form.full_name" required maxlength="100" placeholder="姓名" />
      <input v-model="form.student_id" required maxlength="100" placeholder="学号" />
      <input v-model="form.account" required maxlength="256" autocomplete="off" placeholder="账号" />
      <input v-model="form.password" required type="password" maxlength="1024" autocomplete="new-password" placeholder="密码" />
      <button type="submit" class="btn-ghost">{{ editingId ? '更新账号' : '保存账号' }}</button>
      <button v-if="editingId" type="button" class="btn-ghost" @click="cancelEdit">取消编辑</button>
    </form>
    <p v-if="message" class="modal-msg" :class="{ err: failed }">{{ message }}</p>
  </ModalDialog>
</template>

<style scoped>
.credential-panel { gap: 6px; }
.credential-row { display: flex; justify-content: space-between; align-items: center; color: var(--text-dim); font-size: 12px; }
</style>
