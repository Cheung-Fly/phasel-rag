/* ============================================================
   useSessions —— 会话列表（索引）与当前会话 ID
   ------------------------------------------------------------
   职责边界（重要）：
     本模块只管「有哪些会话」和「当前是哪一个」，
     不碰消息内容。消息由 useChat 持有。

     为什么不合并成一个：会话列表在没有打开任何会话时也要显示
     （侧栏一直可见），而消息列表只在对话区用。两者生命周期不同，
     合并会让「只想刷新一下列表」这种操作牵动消息状态。

   数据来源有两份，且必须以服务器为准：
     服务器  /api/sessions     → 真相（PostgreSQL 里的会话存档）
     本地    localStorage      → 缓存 + 用户自起的标题
     reconcile() 负责合并，规则见 lib/storage.js。
   ============================================================ */
import { ref } from 'vue';
import { api } from '../lib/api.js';
import * as storage from '../lib/storage.js';
import { useToast } from './useToast.js';

const sessions = ref([]);
const currentId = ref(storage.getCurrent());

export function useSessions() {
  const { toast } = useToast();

  /** 从服务器拉取会话列表并校正本地索引 */
  async function sync() {
    try {
      const d = await api.listSessions();
      sessions.value = storage.reconcile(d.sessions || []);
    } catch {
      // 服务器取不到就先用本地的，至少别让列表空着
      sessions.value = storage.getSessions();
    }
    return sessions.value;
  }

  /** 新增或更新一条索引（每次问答结束后调用，用第一个问题当标题） */
  function upsert(id, title, ts) {
    if (!id) return;
    storage.upsertSession(id, title, ts);
    sessions.value = storage.getSessions();
  }

  /**
   * 重命名。
   * ★ 必须打 renamed 标记：否则下一次 sync() 时 reconcile 会用服务器的
   *   标题（第一个问题）把用户起的名字覆盖回去，用户会觉得「改了没用」。
   */
  function rename(id, title) {
    const list = storage.getSessions();
    const item = list.find((s) => s.id === id);
    if (!item) return;
    item.title = title.trim() || item.title;
    item.renamed = true;
    storage.saveSessions(list);
    sessions.value = [...list];
  }

  function removeLocal(id) {
    storage.removeSession(id);
    sessions.value = storage.getSessions();
  }

  function setCurrent(id) {
    currentId.value = id || null;
    storage.setCurrent(id || null);
  }

  function clearLocal() {
    storage.saveSessions([]);
    sessions.value = [];
  }

  /** 清空服务器上的全部会话，返回清掉的条数 */
  async function clearAllRemote() {
    const d = await api.clearSessions();
    clearLocal();
    return d.cleared ?? 0;
  }

  /** 导出某段会话为 Markdown 文件 */
  async function exportSession(id) {
    const d = await api.history(id);
    const history = d.history || [];
    if (!history.length) throw new Error('这段会话没有内容可导出');
    storage.download(`对话-${id.slice(0, 8)}.md`, storage.toMarkdown(id, history, null));
  }

  return {
    sessions, currentId,
    sync, upsert, rename, removeLocal, setCurrent, clearLocal, clearAllRemote, exportSession,
  };
}
