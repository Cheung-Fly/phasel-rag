/* ============================================================
   store.js — 浏览器本地存储
   ------------------------------------------------------------
   存三类东西：
     1. 会话列表索引（session_id + 标题 + 时间）
     2. 上一次用的 session_id
     3. 主题偏好

   ⚠️ 一个必须理解的设计约束
   ------------------------------------------------------------
   后端目前的记忆存在「进程内字典」里（memory_backend = inproc）。
   这意味着：**容器一重启，服务器端的对话记忆就全没了**。

   但 localStorage 是不会消失的。如果只依赖本地存储，
   就会出现「本地列表里有一段会话，点进去却是空的」——
   用户会以为坏了。

   所以 reconcile() 会用服务器返回的 /api/sessions 去校正本地列表：
   服务器没有的，说明记忆已经丢了，就从本地列表里剔除。
   这样本地索引始终反映「真实还能打开的会话」。
   ============================================================ */
'use strict';

const Store = (() => {

  const K_SESSIONS = 'rag.sessions';
  const K_CURRENT  = 'rag.currentSession';
  const K_THEME    = 'rag.theme';

  /* ---------- 底层读写（localStorage 可能被禁用，全部包 try） ---------- */
  function read(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;   // 隐私模式 / 配额满 / 数据损坏
    }
  }

  function write(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch {
      return false;
    }
  }

  /* ---------- 主题 ---------- */
  function getTheme() {
    return read(K_THEME, null);   // null = 跟随系统
  }
  function setTheme(t) {
    if (t) write(K_THEME, t);
    else { try { localStorage.removeItem(K_THEME); } catch {} }
  }

  /* ---------- 会话列表 ---------- */
  function getSessions() {
    const list = read(K_SESSIONS, []);
    return Array.isArray(list) ? list : [];
  }

  function saveSessions(list) {
    write(K_SESSIONS, list.slice(0, 50));   // 最多留 50 条，防止无限增长
  }

  /** 新增或更新一条会话索引 */
  function upsertSession(sessionId, title, ts) {
    if (!sessionId) return;
    const list = getSessions();
    const i = list.findIndex((s) => s.id === sessionId);
    if (i >= 0) {
      list[i].ts = ts || Date.now();
      if (title) list[i].title = title;
    } else {
      list.unshift({ id: sessionId, title: title || '新会话', ts: ts || Date.now() });
    }
    list.sort((a, b) => b.ts - a.ts);
    saveSessions(list);
  }

  /** 重命名（用户可以给会话起好记的名字） */
  function renameSession(sessionId, title) {
    const list = getSessions();
    const item = list.find((s) => s.id === sessionId);
    if (item) { item.title = title; saveSessions(list); }
  }

  function removeSession(sessionId) {
    saveSessions(getSessions().filter((s) => s.id !== sessionId));
  }

  /**
   * 用服务器返回的会话列表校正本地索引。
   *
   * @param {Array} serverSessions  /api/sessions 的返回
   * @returns {Array} 校正后的本地列表
   *
   * 规则：
   *   - 服务器有、本地没有 → 补进本地（例如换了浏览器，或本地被清过）
   *   - 服务器有、本地也有 → 以服务器时间和轮数为准（那是真相）
   *   - 本地有、服务器没有 → 服务器记忆已丢，从本地剔除（避免点进去是空的）
   */
  function reconcile(serverSessions) {
    const serverMap = new Map((serverSessions || []).map((s) => [s.session_id, s]));
    const local = getSessions();

    // 以服务器为准，重建列表
    const rebuilt = [];
    for (const [sid, srv] of serverMap) {
      const localItem = local.find((x) => x.id === sid);
      rebuilt.push({
        id: sid,
        // 本地改过名字就保留用户的命名，否则用第一个问题当标题
        title: localItem?.title || srv.first_question || '新会话',
        // 本地标记过「已重命名」的，不要被服务器标题覆盖
        renamed: localItem?.renamed || false,
        ts: (srv.last_ts ? srv.last_ts * 1000 : Date.now()),
        turns: srv.turns,
      });
    }
    rebuilt.sort((a, b) => b.ts - a.ts);
    saveSessions(rebuilt);
    return rebuilt;
  }

  /* ---------- 当前会话 ---------- */
  function getCurrent() { return read(K_CURRENT, null); }
  function setCurrent(id) {
    if (id) write(K_CURRENT, id);
    else { try { localStorage.removeItem(K_CURRENT); } catch {} }
  }

  /* ---------- 导出 ---------- */
  /** 把一段对话导出成 Markdown 文本 */
  function toMarkdown(sessionId, history, sourcesByIndex) {
    const lines = [
      `# 对话记录`,
      ``,
      `- 会话 ID：\`${sessionId}\``,
      `- 导出时间：${new Date().toLocaleString('zh-CN')}`,
      `- 轮数：${history.length}`,
      ``,
      `---`,
      ``,
    ];
    for (const h of history) {
      lines.push(`## 提问`, ``, h.user, ``, `## 回答`, ``, h.ai, ``);
      // 如果这次问答带来源，一并导出（可选）
      const src = sourcesByIndex?.[h.ts];
      if (src && src.length) {
        lines.push(`<details><summary>引用片段 (${src.length})</summary>`, ``);
        for (const s of src) {
          lines.push(`- **${s.filename}** (score ${s.score})：${s.preview}`);
        }
        lines.push(``, `</details>`, ``);
      }
      lines.push(`---`, ``);
    }
    return lines.join('\n');
  }

  function download(filename, text) {
    const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // 及时释放，否则这块内存要等到页面卸载才回收
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return {
    getTheme, setTheme,
    getSessions, saveSessions, upsertSession, renameSession, removeSession, reconcile,
    getCurrent, setCurrent,
    toMarkdown, download,
  };
})();
