/* ============================================================
   app.js — 主逻辑（编排各模块）
   ------------------------------------------------------------
   依赖（都在 index.html 里按顺序引入）：
     markdown.js  渲染
     sse.js       流式请求
     store.js     本地存储
   ============================================================ */
'use strict';

/* ------------------------------------------------------------
   1. 状态
   ------------------------------------------------------------ */
let sessionId   = null;    // 当前会话 id
let busy        = false;   // 是否正在生成
let controller  = null;    // AbortController，用于「停止生成」
let lastAsk     = null;    // 上一次的提问，用于「重新生成」

// 每次问答的性能统计，用于「重新生成」时对比
const metrics = { ttft: 0, total: 0, chunks: 0, chars: 0 };

const $ = (id) => document.getElementById(id);
const el = {
  messages:   $('messages'),
  composer:   $('composer'),
  question:   $('question'),
  btnSend:    $('btnSend'),
  btnStop:    $('btnStop'),
  btnNewChat: $('btnNewChat'),
  sessionTag: $('sessionTag'),
  sessionList:$('sessionList'),
  btnClearAll:$('btnClearAll'),
  docList:    $('docList'),
  docCount:   $('docCount'),
  fileInput:  $('fileInput'),
  dropzone:   $('dropzone'),
  uploadProg: $('uploadProgress'),
  upName:     $('upName'),
  upBarFill:  $('upBarFill'),
  upStatus:   $('upStatus'),
  health:     $('health'),
  btnTheme:   $('btnTheme'),
  toast:      $('toast'),
  sidebar:    $('sidebar'),
  btnMenu:    $('btnMenu'),
};

/* ------------------------------------------------------------
   2. 通用 UI 辅助
   ------------------------------------------------------------ */

let toastTimer = null;
function toast(msg, kind = 'info') {
  el.toast.textContent = msg;
  el.toast.className = 'toast show' + (kind === 'error' ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.className = 'toast'; }, 2600);
}

function scrollToBottom(force = false) {
  const box = el.messages;
  // 只有用户本来就在底部附近时才自动滚动，
  // 否则会打断「往上翻看历史」的操作 —— 这是聊天界面的重要细节
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  if (force || nearBottom) box.scrollTop = box.scrollHeight;
}

function autoGrow() {
  el.question.style.height = 'auto';
  el.question.style.height = Math.min(el.question.scrollHeight, 180) + 'px';
}

function fmtTime(ts) {
  const d = new Date(ts);
  const now = Date.now();
  const diff = now - ts;
  if (diff < 60_000) return '刚刚';
  if (diff < 3600_000) return Math.floor(diff / 60_000) + ' 分钟前';
  if (diff < 86400_000) return Math.floor(diff / 3600_000) + ' 小时前';
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/* ------------------------------------------------------------
   3. 消息渲染
   ------------------------------------------------------------ */

function clearMessages() { el.messages.innerHTML = ''; }

/**
 * 渲染一条消息。
 * @param {'user'|'bot'} role
 * @param {string} text      用户文本 或 助手回答（Markdown）
 * @param {object} opts      { sources, meta, error, raw }
 */
function addMessage(role, text, opts = {}) {
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-' + role;

  const bubble = document.createElement('div');
  bubble.className = 'bubble' + (opts.error ? ' error' : '');

  // 内容区
  const body = document.createElement('div');
  body.className = 'md-body';
  if (role === 'user') body.textContent = text;          // 用户输入永远当纯文本
  else body.innerHTML = MD.render(text);
  bubble.appendChild(body);

  // 来源
  if (opts.sources && opts.sources.length) {
    bubble.appendChild(renderSources(opts.sources));
  }

  // 助手消息底部工具条（复制 / 重新生成 / 统计）
  if (role === 'bot' && !opts.error) {
    bubble.appendChild(renderTools(text, opts));
  }

  wrap.appendChild(bubble);
  el.messages.appendChild(wrap);
  scrollToBottom(true);
  return { wrap, bubble, body };
}

/** 助手消息的工具条 */
function renderTools(text, opts) {
  const bar = document.createElement('div');
  bar.className = 'msg-tools';

  // 复制
  const copy = document.createElement('button');
  copy.className = 'tool';
  copy.textContent = '复制';
  copy.title = '复制回答内容';
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = '已复制';
      setTimeout(() => (copy.textContent = '复制'), 1200);
    } catch {
      // clipboard API 在非 HTTPS 或旧浏览器下不可用，退回 execCommand
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); copy.textContent = '已复制'; } catch { toast('复制失败', 'error'); }
      ta.remove();
      setTimeout(() => (copy.textContent = '复制'), 1200);
    }
  };
  bar.appendChild(copy);

  // 重新生成（只对最后一条有效）
  const regen = document.createElement('button');
  regen.className = 'tool';
  regen.textContent = '重新生成';
  regen.title = '用同一个问题再问一次';
  regen.onclick = () => {
    if (busy) return toast('正在生成中，请先停止', 'error');
    if (!opts.question) return toast('无法重新生成', 'error');
    // 删掉这条及其后所有消息，然后重发
    let node = bar.closest('.msg');
    while (node) { const nxt = node.nextSibling; node.remove(); node = nxt; }
    send(opts.question, { skipUserBubble: false });
  };
  bar.appendChild(regen);

  // 性能统计
  if (opts.meta) {
    const m = document.createElement('span');
    m.className = 'tool-meta';
    m.textContent = opts.meta;
    m.title = '首字延迟 / 总耗时 / 分块数';
    bar.appendChild(m);
  }

  return bar;
}

function renderSources(sources) {
  const box = document.createElement('details');
  box.className = 'sources';
  const rows = sources.map((s) => `
    <div class="source">
      <span class="source-idx">[${s.index}]</span>
      <span class="source-body">
        <span class="source-file">${MD.esc(s.filename || '未知')}
          <span class="score">${s.score}</span></span>
        <span class="source-preview">${MD.esc(s.preview || '')}</span>
      </span>
    </div>`).join('');
  box.innerHTML =
    `<summary>引用 ${sources.length} 个片段</summary><div class="source-list">${rows}</div>`;
  return box;
}

/**
 * 创建一个「正在生成」的助手气泡，返回句柄。
 * 流式渲染的关键：内容随 token 不断更新。
 */
function createStreamingBubble() {
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-bot';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  const body = document.createElement('div');
  body.className = 'md-body';
  const cursor = document.createElement('span');
  cursor.className = 'cursor';

  body.appendChild(cursor);
  bubble.appendChild(body);
  wrap.appendChild(bubble);
  el.messages.appendChild(wrap);
  scrollToBottom(true);

  let raw = '';
  let renderTimer = null;

  return {
    wrap, bubble, body,
    /**
     * 追加 token。
     * ★ 性能优化：不每个 token 都重渲染 Markdown。
     *   一次回答可能有上百个 token，每次都跑一遍完整解析会明显卡顿。
     *   这里用 requestAnimationFrame 节流 —— 每帧最多渲染一次。
     */
    append(token) {
      raw += token;
      if (renderTimer) return;
      renderTimer = requestAnimationFrame(() => {
        renderTimer = null;
        // 渲染时保留光标在末尾
        body.innerHTML = MD.render(raw);
        body.appendChild(cursor);
        scrollToBottom();
      });
    },
    getText() { return raw; },
    finish(sources, meta, question) {
      if (renderTimer) { cancelAnimationFrame(renderTimer); renderTimer = null; }
      cursor.remove();
      body.innerHTML = MD.render(raw);
      if (sources && sources.length) bubble.appendChild(renderSources(sources));
      bubble.appendChild(renderTools(raw, { meta, question }));
      scrollToBottom();
    },
    fail(msg) {
      if (renderTimer) { cancelAnimationFrame(renderTimer); renderTimer = null; }
      cursor.remove();
      bubble.classList.add('error');
      body.innerHTML = MD.render(msg);
      scrollToBottom();
    },
  };
}

/* ------------------------------------------------------------
   4. 发送 / 流式接收
   ------------------------------------------------------------ */

async function send(text, opts = {}) {
  if (busy) return;
  const question = (text || '').trim();
  if (!question) return;

  busy = true;
  lastAsk = question;
  el.btnSend.disabled = true;
  el.btnStop.hidden = false;

  if (!opts.skipUserBubble) addMessage('user', question);
  el.question.value = '';
  autoGrow();

  const bot = createStreamingBubble();
  controller = new AbortController();

  let sources = [];
  let chunks = 0;
  let chars = 0;
  const t0 = performance.now();
  let tFirst = null;

  try {
    await SSE.post('/api/chat/stream',
      { question, session_id: sessionId || '' },
      (ev) => {
        switch (ev.type) {
          case 'meta':
            if (ev.session_id && ev.session_id !== sessionId) {
              sessionId = ev.session_id;
              el.sessionTag.textContent = sessionId.slice(0, 8);
              Store.setCurrent(sessionId);
            }
            break;

          case 'sources':
            sources = ev.sources || [];
            break;

          case 'token':
            if (tFirst === null) tFirst = performance.now();
            chunks++;
            chars += (ev.text || '').length;
            bot.append(ev.text);
            break;

          case 'done':
            break;

          case 'error':
            bot.fail('生成出错：' + ev.message);
            break;
        }
      },
      controller.signal
    );

    const total = performance.now() - t0;
    metrics.ttft = tFirst ? tFirst - t0 : total;
    metrics.total = total;
    metrics.chunks = chunks;
    metrics.chars = chars;

    const meta = `${(metrics.ttft / 1000).toFixed(2)}s 首字 · ${(total / 1000).toFixed(2)}s 总 · ${chunks} 块 · ${chars} 字`;
    bot.finish(sources, meta, question);

    // 会话索引：用第一个问题当标题
    Store.upsertSession(sessionId, question.slice(0, 30), Date.now());
    renderSessionList(Store.getSessions());

  } catch (e) {
    if (e.name === 'AbortError') {
      // 用户点了「停止生成」：保留已生成的部分，这是符合直觉的
      const partial = bot.getText();
      bot.finish(sources, '已停止', question);
      if (partial) {
        Store.upsertSession(sessionId, question.slice(0, 30), Date.now());
        renderSessionList(Store.getSessions());
      }
      toast('已停止生成');
    } else {
      bot.fail('请求失败：' + e.message);
      toast('请求失败：' + e.message, 'error');
    }
  } finally {
    busy = false;
    controller = null;
    el.btnSend.disabled = false;
    el.btnStop.hidden = true;
    el.question.focus();
  }
}

function stopGenerating() {
  if (controller) controller.abort();
}

/* ------------------------------------------------------------
   5. 文档管理
   ------------------------------------------------------------ */

async function refreshDocs() {
  try {
    const r = await fetch('/api/documents');
    const d = await r.json();
    const docs = d.documents || [];
    el.docCount.textContent = docs.length;

    if (!docs.length) {
      el.docList.innerHTML = '<li class="empty">还没有文档</li>';
      return;
    }
    el.docList.innerHTML = docs.map((doc) => `
      <li>
        <span class="doc-name" title="${MD.esc(doc.filename)}">${MD.esc(doc.filename)}</span>
        <button class="doc-del" data-id="${MD.esc(doc.doc_id)}" title="删除">×</button>
      </li>`).join('');
  } catch (e) {
    el.docList.innerHTML = `<li class="empty">读取失败：${MD.esc(e.message)}</li>`;
  }
}

async function deleteDoc(docId) {
  if (!confirm('确定删除这份文档？对应的向量数据会一起删除，不可恢复。')) return;
  await fetch('/api/documents/' + encodeURIComponent(docId), { method: 'DELETE' });
  toast('已删除');
  refreshDocs();
}

/** 上传用 XMLHttpRequest，因为需要 upload.onprogress（fetch 没有上传进度） */
function uploadFile(file) {
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) return toast('文件超过 20MB 限制', 'error');

  el.uploadProg.hidden = false;
  el.upName.textContent = file.name;
  el.upBarFill.style.width = '0%';
  el.upStatus.textContent = '上传中…';

  const fd = new FormData();
  fd.append('file', file);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/documents/upload');

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = (e.loaded / e.total) * 100;
      el.upBarFill.style.width = pct + '%';
      el.upStatus.textContent = `上传中… ${pct.toFixed(0)}%`;
    }
  };

  xhr.onload = () => {
    el.upBarFill.style.width = '100%';
    if (xhr.status >= 200 && xhr.status < 300) {
      let info = {};
      try { info = JSON.parse(xhr.responseText); } catch {}
      const n = info.chunks ?? '?';
      el.upStatus.textContent = `入库完成：${n} 个片段`;
      toast(`已入库 ${info.filename || file.name}（${n} 片段）`);
      refreshDocs();
    } else {
      let msg = xhr.responseText;
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
      el.upStatus.textContent = '失败：' + msg;
      toast('入库失败：' + msg, 'error');
    }
    el.fileInput.value = '';
  };

  xhr.onerror = () => {
    el.upStatus.textContent = '网络错误';
    toast('上传网络错误', 'error');
    el.fileInput.value = '';
  };

  xhr.send(fd);
}

/* ------------------------------------------------------------
   6. 会话列表
   ------------------------------------------------------------ */

function renderSessionList(list) {
  if (!list.length) {
    el.sessionList.innerHTML = '<li class="empty">还没有会话</li>';
    return;
  }
  el.sessionList.innerHTML = list.map((s) => `
    <li class="session-item${s.id === sessionId ? ' active' : ''}" data-id="${MD.esc(s.id)}">
      <div class="s-title" title="${MD.esc(s.title)}">${MD.esc(s.title)}</div>
      <div class="s-meta">${fmtTime(s.ts)}${s.turns ? ` · ${s.turns} 轮` : ''}</div>
      <div class="s-ops">
        <button class="s-op" data-op="rename" title="重命名">改</button>
        <button class="s-op" data-op="delete" title="删除这段会话">删</button>
      </div>
    </li>`).join('');
}

/** 从服务器拉取会话列表并校正本地索引 */
async function syncSessions() {
  try {
    const r = await fetch('/api/sessions');
    const d = await r.json();
    const list = Store.reconcile(d.sessions || []);
    renderSessionList(list);
    return list;
  } catch (e) {
    // 服务器取不到就先用本地的，至少别让列表空着
    const list = Store.getSessions();
    renderSessionList(list);
    return list;
  }
}

/** 打开一段历史会话 */
async function openSession(id) {
  if (busy) return toast('正在生成中，请先停止', 'error');
  try {
    const r = await fetch(`/api/chat/${encodeURIComponent(id)}/history`);
    const d = await r.json();
    const history = d.history || [];

    sessionId = id;
    Store.setCurrent(id);
    el.sessionTag.textContent = id.slice(0, 8);
    clearMessages();

    if (!history.length) {
      toast('这段会话的记忆已丢失（容器可能重启过）', 'error');
      return;
    }

    for (const h of history) {
      addMessage('user', h.user);
      addMessage('bot', h.ai, { question: h.user });
    }
    renderSessionList(Store.getSessions());
    toast(`已载入 ${history.length} 轮对话`);
  } catch (e) {
    toast('载入失败：' + e.message, 'error');
  }
}

async function deleteSession(id) {
  if (!confirm('删除这段会话？只影响对话记忆，不影响知识库文档。')) return;
  try {
    await fetch(`/api/chat/${encodeURIComponent(id)}/history`, { method: 'DELETE' });
  } catch {}
  Store.removeSession(id);
  if (id === sessionId) newChat();
  syncSessions();
  toast('会话已删除');
}

async function exportSession(id) {
  try {
    const r = await fetch(`/api/chat/${encodeURIComponent(id)}/history`);
    const d = await r.json();
    const history = d.history || [];
    if (!history.length) return toast('这段会话没有内容可导出', 'error');
    const md = Store.toMarkdown(id, history, null);
    Store.download(`对话-${id.slice(0, 8)}.md`, md);
    toast('已导出为 Markdown');
  } catch (e) {
    toast('导出失败：' + e.message, 'error');
  }
}

/* ------------------------------------------------------------
   7. 新会话 / 主题 / 健康检查
   ------------------------------------------------------------ */

function newChat() {
  sessionId = null;
  Store.setCurrent(null);
  el.sessionTag.textContent = '—';
  clearMessages();
  addMessage('bot',
    '已开启新会话。\n\n之前的对话记忆不再参与，但**知识库文档仍然可用**。');
  renderSessionList(Store.getSessions());
  el.question.focus();
  el.sidebar.classList.remove('open');
}

function applyTheme(theme) {
  // theme: 'light' | 'dark' | null(跟随系统)
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  const isLight = theme === 'light' ||
    (!theme && matchMedia('(prefers-color-scheme: light)').matches);
  el.btnTheme.textContent = isLight ? '☾' : '☀';
  el.btnTheme.title = isLight ? '切换到深色' : '切换到浅色';
}

function toggleTheme() {
  const cur = Store.getTheme();
  const isLightNow = cur === 'light' ||
    (!cur && matchMedia('(prefers-color-scheme: light)').matches);
  const next = isLightNow ? 'dark' : 'light';
  Store.setTheme(next);
  applyTheme(next);
  toast(next === 'light' ? '已切换为浅色' : '已切换为深色');
}

async function checkHealth() {
  try {
    const r = await fetch('/health');
    const d = await r.json();
    if (d.status === 'ok') {
      el.health.innerHTML =
        `<span class="dot dot-ok"></span><span>${MD.esc(d.llm_model)}</span>` +
        `<span class="mem-tag" title="记忆后端：inproc 表示容器重启后会丢失">${MD.esc(d.memory_backend)}</span>`;
    } else {
      el.health.innerHTML = `<span class="dot dot-err"></span><span>异常</span>`;
    }
  } catch {
    el.health.innerHTML = `<span class="dot dot-err"></span><span>无法连接</span>`;
  }
}

/* ------------------------------------------------------------
   8. 事件绑定
   ------------------------------------------------------------ */

el.composer.addEventListener('submit', (e) => {
  e.preventDefault();
  send(el.question.value);
});

el.question.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send(el.question.value);
  }
});
el.question.addEventListener('input', autoGrow);

el.btnStop.addEventListener('click', stopGenerating);
el.btnNewChat.addEventListener('click', newChat);
el.btnTheme.addEventListener('click', toggleTheme);
el.btnMenu.addEventListener('click', () => el.sidebar.classList.toggle('open'));

el.btnClearAll.addEventListener('click', async () => {
  if (!confirm('清空所有会话记忆？知识库文档不受影响。')) return;
  try {
    const r = await fetch('/api/sessions', { method: 'DELETE' });
    const d = await r.json();
    Store.saveSessions([]);
    newChat();
    syncSessions();
    toast(`已清空 ${d.cleared} 段会话`);
  } catch (e) {
    toast('清空失败：' + e.message, 'error');
  }
});

el.sessionList.addEventListener('click', (e) => {
  const item = e.target.closest('.session-item');
  if (!item) return;
  const id = item.dataset.id;
  const op = e.target.dataset.op;

  if (op === 'delete') return deleteSession(id);
  if (op === 'rename') {
    const cur = Store.getSessions().find((s) => s.id === id);
    const name = prompt('给这段会话起个名字：', cur?.title || '');
    if (name === null) return;
    // 标记 renamed，避免下次 syncSessions 时被服务器的标题覆盖
    const list = Store.getSessions();
    const it = list.find((s) => s.id === id);
    if (it) { it.title = name.trim() || it.title; it.renamed = true; Store.saveSessions(list); }
    renderSessionList(Store.getSessions());
    return;
  }
  // 单击 = 打开；长按/双击 = 导出（双击比较直观）
  openSession(id);
});

// 双击会话项 = 导出
el.sessionList.addEventListener('dblclick', (e) => {
  const item = e.target.closest('.session-item');
  if (item && !e.target.dataset.op) exportSession(item.dataset.id);
});

el.docList.addEventListener('click', (e) => {
  const btn = e.target.closest('.doc-del');
  if (btn) deleteDoc(btn.dataset.id);
});

el.fileInput.addEventListener('change', (e) => uploadFile(e.target.files[0]));

/* 拖拽上传：dragover 必须 preventDefault，否则浏览器会直接打开文件 */
['dragenter', 'dragover'].forEach((ev) =>
  el.dropzone.addEventListener(ev, (e) => { e.preventDefault(); el.dropzone.classList.add('dragover'); })
);
['dragleave', 'drop'].forEach((ev) =>
  el.dropzone.addEventListener(ev, (e) => { e.preventDefault(); el.dropzone.classList.remove('dragover'); })
);
el.dropzone.addEventListener('drop', (e) => {
  const f = e.dataTransfer.files[0];
  if (f) uploadFile(f);
});

/* 全局快捷键 */
document.addEventListener('keydown', (e) => {
  // Ctrl/Cmd + K：聚焦输入框
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault(); el.question.focus(); el.question.select();
  }
  // Esc：停止生成
  if (e.key === 'Escape' && busy) stopGenerating();
  // Ctrl/Cmd + /：切换主题
  if ((e.ctrlKey || e.metaKey) && e.key === '/') { e.preventDefault(); toggleTheme(); }
});

/* 窄屏：点消息区收起侧栏 */
el.messages.addEventListener('click', () => el.sidebar.classList.remove('open'));

/* ------------------------------------------------------------
   9. 启动
   ------------------------------------------------------------ */
(async function init() {
  applyTheme(Store.getTheme());

  await refreshDocs();
  checkHealth();
  setInterval(checkHealth, 30000);

  const list = await syncSessions();

  // 尝试恢复上次的会话
  const saved = Store.getCurrent();
  if (saved && list.some((s) => s.id === saved)) {
    await openSession(saved);
    // openSession 会打印"已载入"提示，这里不再重复
  } else {
    addMessage('bot',
      '你好，我是基于你上传文档回答问题的助手。\n\n' +
      '1. 先在左侧**上传一份文档**（支持 .md / .txt / .pdf）\n' +
      '2. 然后在下面提问\n\n' +
      '我会尽量只依据文档内容回答；找不到就明说，**不会编造**。');
  }

  el.question.focus();
})();
