<script setup>
/* ============================================================
   SitePanel —— 可操作站点注册表（阶段 C ①）
   ------------------------------------------------------------
   形态取舍：侧栏只放列表 + 一个「新增」，编辑另开浮层。
     一个站点规格有 10 个字段（含两个 CSS 选择器），316px 宽的侧栏
     塞不下可用的表单。列表留在侧栏是为了让「助手现在能操作哪些站点」
     随手可见；编辑是低频动作，适合浮层。

   ★ 浮层必须 Teleport 到 body：
     窄屏时侧栏带 transform（抽屉滑入）。而 position:fixed 的元素若位于
     一个 transform 祖先内部，会以那个祖先为包含块 —— 表现就是浮层
     被裁进侧栏里，而不是覆盖全屏。不写这条注释，下次很容易踩。

   ★ 这里配的是「助手允许访问哪些站点」，不是「助手能不能上网」：
     未登记的站点，浏览器侧白名单直接拒绝，助手连打开都不会尝试。
     所以这个面板是阶段 C「信任面」第一个能被用户看见的东西。
   ============================================================ */
import { useSites } from '../composables/useSites.js';

const {
  sites,
  listError,
  draft,
  editing,
  editingKey,
  saving,
  saveError,
  openCreate,
  openEdit,
  closeEditor,
  save,
  remove,
  toggleEnabled,
} = useSites();
</script>

<template>
  <section class="panel">
    <h3 class="panel-title">
      可操作站点
      <span class="count">{{ sites.length }}</span>
      <button class="link-btn" type="button" @click="openCreate">新增</button>
    </h3>

    <ul class="site-list">
      <!-- 三种状态互斥，顺序有意义：先报错、再空、最后才是列表 -->
      <li v-if="listError" class="empty">读取失败：{{ listError }}</li>
      <li v-else-if="!sites.length" class="empty">还没有站点</li>
      <template v-else>
        <li v-for="site in sites" :key="site.site_key" :class="{ off: !site.enabled }">
          <div class="site-main">
            <span class="site-name" :title="site.host">{{ site.name }}</span>
            <span class="site-host">{{ site.host }}</span>
          </div>
          <button
            class="site-btn"
            type="button"
            :title="site.enabled ? '停用：助手不再操作该站点' : '启用'"
            @click="toggleEnabled(site)"
          >
            {{ site.enabled ? '停用' : '启用' }}
          </button>
          <button class="site-btn" type="button" title="编辑" @click="openEdit(site)">
            编辑
          </button>
          <button
            class="site-btn site-btn-danger"
            type="button"
            title="删除"
            @click="remove(site.site_key)"
          >
            ×
          </button>
        </li>
      </template>
    </ul>

    <p class="site-hint">
      助手只会在这些站点内操作；新增站点无需重启服务。
    </p>
  </section>

  <!-- Teleport 到 body，理由见本文件头部注释 -->
  <Teleport to="body">
    <div v-if="editing" class="site-backdrop" @click.self="closeEditor">
      <section class="site-modal" role="dialog" aria-modal="true">
        <header>
          <h3>{{ editingKey ? '编辑站点' : '新增站点' }}</h3>
          <button type="button" class="btn-ghost" :disabled="saving" @click="closeEditor">
            关闭
          </button>
        </header>

        <div class="site-form">
          <label>
            <span>站点标识（site_key）</span>
            <input
              v-model="draft.site_key"
              :disabled="Boolean(editingKey)"
              placeholder="例如 nsu-smart-education"
            >
            <small v-if="editingKey">
              标识是凭证的锚点，创建后不可修改。要换名请新增后再删除旧站点。
            </small>
            <small v-else>只能用小写字母、数字和连字符。</small>
          </label>

          <label>
            <span>显示名称</span>
            <input v-model="draft.name" placeholder="例如 东软智慧教育平台">
          </label>

          <label>
            <span>触发别名（每行一个，也支持逗号分隔）</span>
            <textarea
              v-model="draft.aliases"
              rows="2"
              placeholder="东软智慧教育平台&#10;智慧教育平台"
            ></textarea>
            <small>说话里包含任一别名，助手才会认为你想登录这个站点。</small>
          </label>

          <label>
            <span>站点域名</span>
            <input v-model="draft.host" placeholder="study.nsu.edu.cn">
            <small>只能是域名，不能填 IP。这同时是浏览器允许访问的范围。</small>
          </label>

          <label>
            <span>登录页地址</span>
            <input v-model="draft.login_url" placeholder="https://study.nsu.edu.cn/login">
            <small>必须是 https，且域名要与上面一致；不一致会被拒绝而不是自动纠正。</small>
          </label>

          <label>
            <span>用户名输入框选择器</span>
            <input v-model="draft.username_selector" placeholder='input[placeholder="请输入用户名"]'>
          </label>

          <label>
            <span>密码输入框选择器</span>
            <input v-model="draft.password_selector" placeholder='input[placeholder="请输入密码"]'>
          </label>

          <label>
            <span>危险路径关键词（每行一个，可留空）</span>
            <textarea
              v-model="draft.blocked_path_parts"
              rows="3"
              placeholder="logout&#10;submit&#10;退选"
            ></textarea>
            <small>路径里含这些词时，助手不会把它当可点链接列出来。</small>
          </label>

          <div class="site-checks">
            <label class="inline">
              <input v-model="draft.requires_captcha" type="checkbox">
              <span>登录页需要人工验证码</span>
            </label>
            <label class="inline">
              <input v-model="draft.enabled" type="checkbox">
              <span>启用该站点</span>
            </label>
          </div>
        </div>

        <p v-if="saveError" class="site-error" role="alert">{{ saveError }}</p>

        <footer>
          <button type="button" class="btn-ghost" :disabled="saving" @click="closeEditor">
            取消
          </button>
          <button type="button" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.site-list { list-style: none; margin: 0; padding: 0; }
.site-list li {
  display: flex; align-items: center; gap: 4px;
  padding: 6px 8px; border-radius: var(--radius-sm);
  font-size: 12.5px; background: var(--bg-input); margin-bottom: 5px;
}
.site-list li.empty {
  background: none; color: var(--text-faint);
  font-size: 11.5px; justify-content: center;
}
.site-list li.off .site-name, .site-list li.off .site-host { opacity: .55; }
.site-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.site-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.site-host {
  font-size: 10.5px; color: var(--text-faint);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.site-btn {
  flex: 0 0 auto;
  border: none; background: none; color: var(--text-faint);
  cursor: pointer; font-size: 11px; line-height: 1; padding: 2px 4px;
  border-radius: 4px;
}
.site-btn:hover { color: var(--text); background: var(--bg-hover); }
.site-btn-danger { font-size: 14px; }
.site-btn-danger:hover { color: var(--err); background: color-mix(in srgb, var(--err) 14%, transparent); }
.site-hint { margin: 0; font-size: 10.5px; color: var(--text-faint); }

/* ---------- 浮层编辑器 ---------- */
.site-backdrop {
  position: fixed; inset: 0; z-index: 40;
  display: grid; place-items: center; padding: 18px;
  background: rgb(0 0 0 / 60%);
  overflow: auto;
}
.site-modal {
  display: grid; gap: 12px;
  width: min(560px, 100%);
  max-height: 92vh; overflow: auto;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  box-shadow: var(--shadow);
}
.site-modal header, .site-modal footer {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
}
.site-modal h3 { margin: 0; font-size: 15px; }
.site-form { display: grid; gap: 11px; }
.site-form label { display: grid; gap: 4px; font-size: 12px; }
.site-form label > span { color: var(--text-dim); }
.site-form small { color: var(--text-faint); font-size: 10.5px; line-height: 1.5; }
.site-form input, .site-form textarea {
  width: 100%; box-sizing: border-box;
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text);
  font: inherit; font-size: 12.5px;
}
.site-form input:disabled { opacity: .65; cursor: not-allowed; }
.site-form textarea { resize: vertical; font-family: inherit; }
.site-checks { display: flex; flex-wrap: wrap; gap: 16px; }
.site-checks label.inline {
  display: flex; align-items: center; gap: 6px; font-size: 12px;
}
.site-checks input { width: auto; }
.site-error { margin: 0; color: var(--err); font-size: 12px; }
.site-modal footer { justify-content: flex-end; }
.site-modal footer button {
  padding: 8px 14px;
  border: 0; border-radius: var(--radius-sm);
  background: var(--accent); color: white; cursor: pointer;
}
.site-modal footer .btn-ghost {
  background: var(--bg-input); color: var(--text-dim);
  border: 1px solid var(--border);
}
.site-modal button:disabled { opacity: .6; cursor: wait; }
.site-modal header .btn-ghost { padding: 5px 10px; font-size: 12px; }
</style>
