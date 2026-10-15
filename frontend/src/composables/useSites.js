/* ============================================================
   useSites —— 可操作站点注册表（阶段 C ①）
   ------------------------------------------------------------
   为什么这份状态放在共享 composable 而不是组件内部：
     站点列表既要在侧栏面板里显示，又要在浮层编辑器里读写。
     两处各持一份 ref 的话，「保存成功但列表没刷新」几乎必然发生。
     这与 useDocuments / useTasks 的取舍一致。

   ★ 前端**不重复实现**后端的校验规则。
     哪些 host 合法、login_url 与 host 必须同域、site_key 的字符集 ——
     这些规则的唯一权威在 site_store.validate_site()。前端只管把
     后端那句中文报错原样显示出来。若在两边各写一套，规则一不一致，
     用户就会遇到「界面说可以、保存说不行」这种最难解释的失败。
     前端唯一做的转换是「多行文本 → 数组」，那是输入形式，不是校验。
   ============================================================ */
import { reactive, ref } from 'vue';
import { api } from '../lib/api.js';
import { useToast } from './useToast.js';

const sites = ref([]);
const listError = ref('');

// 编辑器草稿。用 reactive 是有意的：打开编辑器时要能整体重置，
// 残留上一个站点的字段会让人把两个站点的配置混着提交。
const draft = reactive(emptyDraft());
const editing = ref(false);      // 浮层是否打开
const editingKey = ref('');      // 正在编辑的 site_key；空串 = 新增
const saving = ref(false);
const saveError = ref('');

function emptyDraft() {
  return {
    site_key: '',
    name: '',
    aliases: '',               // 界面用多行文本，提交前拆成数组
    host: '',
    login_url: '',
    username_selector: '',
    password_selector: '',
    blocked_path_parts: '',    // 同上
    requires_captcha: true,
    enabled: true,
  };
}

/** 多行/逗号分隔的文本 → 去空去重的数组。 */
function toList(text) {
  return [...new Set(
    String(text || '')
      .split(/[\n,，]/)
      .map((item) => item.trim())
      .filter(Boolean),
  )];
}

/** 已保存的站点 → 提交体。显式列出字段，避免把 created_at 这类只读字段带上。 */
function siteToPayload(site) {
  return {
    site_key: site.site_key,
    name: site.name,
    aliases: site.aliases || [],
    host: site.host,
    login_url: site.login_url,
    username_selector: site.username_selector,
    password_selector: site.password_selector,
    blocked_path_parts: site.blocked_path_parts || [],
    requires_captcha: site.requires_captcha !== false,
    enabled: site.enabled !== false,
  };
}

export function useSites() {
  const { toast } = useToast();

  async function refresh() {
    try {
      const data = await api.listSites();
      sites.value = data.sites || [];
      listError.value = '';
    } catch (error) {
      sites.value = [];
      listError.value = error.message || '读取失败';
    }
  }

  function openCreate() {
    Object.assign(draft, emptyDraft());
    editingKey.value = '';
    saveError.value = '';
    editing.value = true;
  }

  function openEdit(site) {
    Object.assign(draft, {
      site_key: site.site_key,
      name: site.name,
      aliases: (site.aliases || []).join('\n'),
      host: site.host,
      login_url: site.login_url,
      username_selector: site.username_selector,
      password_selector: site.password_selector,
      blocked_path_parts: (site.blocked_path_parts || []).join('\n'),
      requires_captcha: site.requires_captcha !== false,
      enabled: site.enabled !== false,
    });
    editingKey.value = site.site_key;
    saveError.value = '';
    editing.value = true;
  }

  function closeEditor() {
    if (saving.value) return;
    editing.value = false;
    editingKey.value = '';
    saveError.value = '';
  }

  async function save() {
    if (saving.value) return;
    saving.value = true;
    saveError.value = '';
    const payload = {
      site_key: draft.site_key.trim().toLowerCase(),
      name: draft.name.trim(),
      aliases: toList(draft.aliases),
      host: draft.host.trim(),
      login_url: draft.login_url.trim(),
      username_selector: draft.username_selector.trim(),
      password_selector: draft.password_selector.trim(),
      blocked_path_parts: toList(draft.blocked_path_parts),
      requires_captcha: draft.requires_captcha,
      enabled: draft.enabled,
    };
    const editingExisting = Boolean(editingKey.value);
    try {
      if (editingExisting) {
        await api.updateSite(editingKey.value, payload);
      } else {
        await api.createSite(payload);
      }
    } catch (error) {
      // 422 说的是「哪一项填错了」，409 说的是「这个名字被占了」。
      // 后端已经把它们区分开了，这里原样展示，不吞成一句「保存失败」。
      saveError.value = error.message || '保存失败';
      saving.value = false;
      return;
    }
    saving.value = false;
    toast(editingExisting ? '站点已更新' : '站点已新增');
    editing.value = false;
    editingKey.value = '';
    // ★ 顺序要紧：先关浮层再刷新。刷新失败时用户仍能看到列表里的错误提示，
    //   而不是被一个开着且内容已过期的浮层挡住。
    await refresh();
  }

  async function remove(siteKey) {
    if (!confirm(
      `确定删除站点「${siteKey}」？\n\n`
      + '该站点已登记的凭证不会被删除，但它们会失去归属 —— '
      + '助手会提示「尚未登记该站点的登录凭证」。',
    )) return;
    try {
      await api.deleteSite(siteKey);
      toast('已删除');
      await refresh();
    } catch (error) {
      toast('删除失败：' + (error.message || ''), 'error');
    }
  }

  /** 停用/启用。停用比删除「温和」：配置留着，助手不再碰它。 */
  async function toggleEnabled(site) {
    try {
      await api.updateSite(site.site_key, {
        ...siteToPayload(site),
        enabled: !site.enabled,
      });
      await refresh();
    } catch (error) {
      toast('切换失败：' + (error.message || ''), 'error');
    }
  }

  return {
    sites,
    listError,
    draft,
    editing,
    editingKey,
    saving,
    saveError,
    refresh,
    openCreate,
    openEdit,
    closeEditor,
    save,
    remove,
    toggleEnabled,
  };
}
