<script setup>
import { computed, onMounted } from 'vue';
import { ref } from 'vue';
import { useExtensions } from '../composables/useExtensions.js';
import { api } from '../lib/api.js';

const { snapshot, loading, error, refresh } = useExtensions();
const enabledServers = computed(() => snapshot.value.mcp_servers.filter((item) => item.enabled));
const servers = ref([]);
const serverName = ref('');
const serverUrl = ref('');
const serverError = ref('');

async function loadServers() {
  try {
    servers.value = (await api.mcpServers()).servers || [];
  } catch (exception) {
    serverError.value = exception.message || 'MCP 配置读取失败';
  }
}

async function saveServer() {
  serverError.value = '';
  try {
    await api.saveMcpServer({ name: serverName.value, transport: 'streamable_http', url: serverUrl.value });
    serverName.value = '';
    serverUrl.value = '';
    await Promise.all([refresh(), loadServers()]);
  } catch (exception) {
    serverError.value = exception.message || 'MCP 保存失败';
  }
}

async function removeServer(id) {
  await api.deleteMcpServer(id);
  await Promise.all([refresh(), loadServers()]);
}

async function testServer(id) {
  try {
    const result = await api.testMcpServer(id);
    serverError.value = `${result.server} 已发现 ${result.tool_count} 个工具`;
  } catch (exception) {
    serverError.value = exception.message || 'MCP 测试失败';
  }
}

onMounted(() => Promise.all([refresh(), loadServers()]));
</script>

<template>
  <section class="panel extension-panel">
    <h2 class="panel-title">扩展能力</h2>
    <p v-if="loading" class="extension-muted">正在读取 Skill / MCP…</p>
    <p v-else-if="error" class="extension-error">{{ error }}</p>
    <template v-else>
      <div class="extension-row">
        <span>Skill</span>
        <strong>{{ snapshot.skills.length }}</strong>
      </div>
      <div class="extension-row">
        <span>MCP</span>
        <strong>{{ enabledServers.length }}</strong>
      </div>
      <p class="extension-hint">
        使用 <code>/skill</code> 或 <code>/mcp</code> 调用已启用扩展。
      </p>
      <ul v-if="snapshot.skills.length || enabledServers.length" class="extension-list">
        <li v-for="skill in snapshot.skills" :key="`skill-${skill.name}`">Skill · {{ skill.name }}</li>
        <li v-for="server in enabledServers" :key="`mcp-${server.name}`">
          MCP · {{ server.name }}（{{ server.transport }}）
        </li>
      </ul>
      <form class="mcp-form" @submit.prevent="saveServer">
        <input v-model="serverName" required maxlength="80" placeholder="MCP 名称" />
        <input v-model="serverUrl" required type="url" placeholder="https://example.com/mcp" />
        <button type="submit" class="btn-ghost">添加 HTTP MCP</button>
      </form>
      <p v-if="serverError" class="extension-error">{{ serverError }}</p>
      <ul v-if="servers.length" class="extension-list">
        <li v-for="server in servers" :key="server.server_id">
          {{ server.name }} · {{ server.enabled ? '启用' : '停用' }}
          <button type="button" class="link-button" @click="testServer(server.server_id)">测试</button>
          <button type="button" class="link-button" @click="removeServer(server.server_id)">删除</button>
        </li>
      </ul>
    </template>
    <button type="button" class="btn-ghost extension-refresh" @click="refresh">刷新状态</button>
  </section>
</template>

<style scoped>
.extension-panel { gap: 6px; }
.extension-row { display: flex; justify-content: space-between; color: var(--text-dim); font-size: 12px; }
.extension-row strong { color: var(--text); }
.extension-muted, .extension-error, .extension-hint { margin: 0; font-size: 11px; color: var(--text-faint); }
.extension-error { color: var(--err); }
.extension-hint code { color: var(--accent); }
.extension-list { margin: 2px 0 0; padding-left: 18px; color: var(--text-dim); font-size: 11px; }
.extension-refresh { align-self: flex-start; margin-top: 2px; }
.mcp-form { display: grid; gap: 5px; margin-top: 5px; }
.mcp-form input { min-width: 0; border: 1px solid var(--border); border-radius: 5px; padding: 5px; background: var(--surface); color: var(--text); font-size: 11px; }
.link-button { border: 0; background: transparent; color: var(--accent); cursor: pointer; font-size: 11px; padding: 0 3px; }
</style>
