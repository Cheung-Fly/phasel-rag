<script setup>
import { computed, onMounted, ref } from 'vue';
import { useExtensions } from '../composables/useExtensions.js';
import { api } from '../lib/api.js';
import ModalDialog from './ModalDialog.vue';

const { snapshot, loading, error, refresh } = useExtensions();
const enabledServers = computed(() => snapshot.value.mcp_servers.filter((item) => item.enabled));

const dialog = ref('');
const message = ref('');
const failed = ref(false);

const skills = ref([]);
const skillForm = ref({ name: '', content: '' });
const servers = ref([]);
const serverForm = ref({ name: '', url: '' });

async function loadSkills() {
  skills.value = (await api.skills()).skills || [];
}
async function loadServers() {
  servers.value = (await api.mcpServers()).servers || [];
}

async function run(action, success) {
  message.value = '';
  failed.value = false;
  try {
    const result = await action();
    message.value = typeof success === 'function' ? success(result) : success;
  } catch (exception) {
    failed.value = true;
    message.value = exception.message || '操作失败';
  }
  await Promise.all([refresh(), loadSkills().catch(() => {}), loadServers().catch(() => {})]);
}

function openDialog(kind) {
  message.value = '';
  dialog.value = kind;
}

function editSkill(skill) {
  skillForm.value = { name: skill.name, content: skill.content };
}

function saveSkill() {
  return run(async () => {
    await api.saveSkill(skillForm.value);
    skillForm.value = { name: '', content: '' };
  }, 'Skill 已保存');
}

function saveServer() {
  return run(async () => {
    await api.saveMcpServer({ name: serverForm.value.name, transport: 'streamable_http', url: serverForm.value.url });
    serverForm.value = { name: '', url: '' };
  }, 'MCP 已添加');
}

onMounted(() => Promise.all([refresh(), loadSkills().catch(() => {}), loadServers().catch(() => {})]));
</script>

<template>
  <section class="panel extension-panel">
    <h2 class="panel-title">扩展能力</h2>
    <p v-if="loading && !snapshot.skills.length" class="extension-muted">正在读取 Skill / MCP…</p>
    <p v-else-if="error" class="extension-error">{{ error }}</p>
    <template v-else>
      <div class="extension-row">
        <span>Skill · {{ snapshot.skills.length }}</span>
        <button type="button" class="btn-ghost" @click="openDialog('skill')">管理</button>
      </div>
      <div class="extension-row">
        <span>MCP · {{ enabledServers.length }}</span>
        <button type="button" class="btn-ghost" @click="openDialog('mcp')">管理</button>
      </div>
      <p class="extension-hint">
        使用 <code>/skill</code> 或 <code>/mcp</code> 调用已启用扩展。
      </p>
    </template>
  </section>

  <ModalDialog v-if="dialog === 'skill'" title="管理 Skill" @close="dialog = ''">
    <h4>已添加的 Skill</h4>
    <ul v-if="skills.length" class="modal-list">
      <li v-for="skill in skills" :key="skill.name">
        <span>{{ skill.name }}</span>
        <button type="button" class="link-button" @click="editSkill(skill)">编辑</button>
        <button type="button" class="link-button" @click="run(() => api.deleteSkill(skill.name), '已删除')">删除</button>
      </li>
    </ul>
    <p v-else class="modal-msg">还没有 Skill。</p>
    <h4>添加 / 编辑 Skill</h4>
    <form class="modal-form" @submit.prevent="saveSkill">
      <input v-model="skillForm.name" required maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_\-]*" placeholder="名称（字母、数字、下划线、连字符）" />
      <textarea v-model="skillForm.content" required maxlength="12000" placeholder="SKILL.md 内容：说明这个 Skill 做什么、怎么做"></textarea>
      <button type="submit" class="btn-ghost">保存 Skill</button>
    </form>
    <p v-if="message" class="modal-msg" :class="{ err: failed }">{{ message }}</p>
  </ModalDialog>

  <ModalDialog v-if="dialog === 'mcp'" title="管理 MCP" @close="dialog = ''">
    <h4>已添加的 MCP</h4>
    <ul v-if="servers.length" class="modal-list">
      <li v-for="server in servers" :key="server.server_id">
        <span>{{ server.name }} · {{ server.enabled ? '启用' : '停用' }}</span>
        <button type="button" class="link-button" @click="run(() => api.testMcpServer(server.server_id), (r) => `${r.server} 已发现 ${r.tool_count} 个工具`)">测试</button>
        <button type="button" class="link-button" @click="run(() => api.toggleMcpServer(server.server_id, !server.enabled), server.enabled ? '已停用' : '已启用')">{{ server.enabled ? '停用' : '启用' }}</button>
        <button type="button" class="link-button" @click="run(() => api.deleteMcpServer(server.server_id), '已删除')">删除</button>
      </li>
    </ul>
    <p v-else class="modal-msg">还没有 MCP 服务。</p>
    <h4>添加 HTTP MCP</h4>
    <form class="modal-form" @submit.prevent="saveServer">
      <input v-model="serverForm.name" required maxlength="80" placeholder="MCP 名称" />
      <input v-model="serverForm.url" required type="url" placeholder="https://example.com/mcp" />
      <button type="submit" class="btn-ghost">添加 MCP</button>
    </form>
    <h4>内置网页抓取</h4>
    <p class="modal-msg">Fetch 会读取公开网页并转成 Markdown；私网地址默认拦截。</p>
    <button type="button" class="btn-ghost" @click="run(() => api.addBuiltinFetch(), 'Fetch MCP 已添加')">
      添加 Fetch 网页内容抓取
    </button>
    <p v-if="message" class="modal-msg" :class="{ err: failed }">{{ message }}</p>
  </ModalDialog>
</template>

<style scoped>
.extension-panel { gap: 6px; }
.extension-row { display: flex; justify-content: space-between; align-items: center; color: var(--text-dim); font-size: 12px; }
.extension-muted, .extension-error, .extension-hint { margin: 0; font-size: 11px; color: var(--text-faint); }
.extension-error { color: var(--err); }
.extension-hint code { color: var(--accent); }
</style>
