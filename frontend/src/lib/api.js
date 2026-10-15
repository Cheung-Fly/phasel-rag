/* ============================================================
   api.js —— 后端接口的唯一出口
   ------------------------------------------------------------
   为什么要把所有 fetch 收到一个文件里：
     迁移前每个函数各自写 fetch('/api/...')，路径散落在各处。
     一旦后端改路径、或要给所有请求统一加 Header / 统一处理 401，
     就得全项目搜索替换，且极易漏。
     收拢之后，「前端用到哪些后端能力」一眼可查，也便于对照
     FastAPI 的 /openapi.json 逐条核对。

   ★ 为什么请求都用「相对路径」：
     /api/... 而不是 http://某主机:8000/api/...
     这样开发期由 Vite 代理转发、生产期由 nginx 转发，
     浏览器眼里始终是同源请求。同一份代码在两种环境下行为一致，
     也不需要为了上线再去改地址（更不需要配 CORS）。
   ============================================================ */

/** 统一的响应处理：失败时尽量把后端的 detail 提出来 */
async function asJson(resp) {
  if (!resp.ok) {
    let detail = '';
    try {
      const j = await resp.json();
      detail = j.detail || JSON.stringify(j);
    } catch {
      detail = await resp.text().catch(() => '');
    }
    throw new Error(detail || `HTTP ${resp.status} ${resp.statusText}`);
  }
  return resp.json();
}

const json = (url, init) =>
  fetch(url, { credentials: 'same-origin', ...init }).then(asJson);

export const api = {
  /* ---------- 基础 ---------- */
  health: () => json('/health'),

  /* ---------- 本机主人身份 ---------- */
  // 2026-10-14：单用户本地模式后，前端不再有登录/退出动作，因此
  // authLogin / authLogout 已删除（后端在 local 模式下也会对它们返回 404，
  // 不存在「前端不调但接口还在偷偷签发会话」的中间态）。
  // 保留 authMe 是因为它的 user_id 被用作 localStorage 的命名空间，
  // 见 App.vue 的 bootstrap() 与 lib/storage.js 的 setUserScope。
  authMe: () => json('/api/auth/me'),

  /* ---------- 隔离浏览器人工验证 ---------- */
  browserSession: () => json('/api/browser-session'),

  browserSessionAction: (action, { x = -1, y = -1, key = '' } = {}) =>
    json('/api/browser-session/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, x, y, key }),
    }),

  completeBrowserVerification: () =>
    json('/api/browser-session/complete', { method: 'POST' }),

  closeBrowserSession: () =>
    json('/api/browser-session', { method: 'DELETE' }),

  /* ---------- 文档 ---------- */
  listDocuments: () => json('/api/documents'),

  deleteDocument: (docId) =>
    json(`/api/documents/${encodeURIComponent(docId)}`, { method: 'DELETE' }),

  /* ---------- 后台任务（2026-10-14 阶段 B） ---------- */
  // 上传不再是「一次请求换一个结果」，而是「提交任务 + 轮询状态」。
  // 这三个接口就是那个「查状态」的通道。
  listTasks: (limit = 20) => json(`/api/tasks?limit=${encodeURIComponent(limit)}`),

  getTask: (taskId) => json(`/api/tasks/${encodeURIComponent(taskId)}`),

  cancelTask: (taskId) =>
    json(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }),

  /**
   * 上传文档。
   * ★ 这里坚持用 XMLHttpRequest 而不是 fetch：fetch 至今没有「上传进度」事件，
   *   而上传一份文档可能几 MB~几十 MB，没有进度条用户会以为卡死。
   *   fetch 只有下行的 ReadableStream 进度，上行进度在 Web 平台只有 XHR 提供。
   *
   * ★ 响应语义（2026-10-14 起）：成功返回 {mode:'queued', task_id, ...} 表示
   *   「文件已收到，入库已排进后台任务」，【不代表入库完成】；progress 回调
   *   反映的也仅是字节上传进度。入库进度改由 /api/tasks 提供。
   *   降级时后端返回 {mode:'sync', chunks, ...}，那才是「已完成」的语义。
   */
  uploadDocument(file, { onProgress } = {}) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append('file', file);

      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/documents/upload');

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress((e.loaded / e.total) * 100);
      };

      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          let info = {};
          try { info = JSON.parse(xhr.responseText); } catch { /* 空响应也当成功 */ }
          resolve(info);
        } else {
          let msg = xhr.responseText;
          try { msg = JSON.parse(xhr.responseText).detail || msg; } catch { /* 保留原文 */ }
          reject(new Error(msg || `HTTP ${xhr.status}`));
        }
      };

      xhr.onerror = () => reject(new Error('网络错误'));
      xhr.send(fd);
    });
  },

  /* ---------- 站点注册表（2026-10-15 阶段 C ①） ---------- */
  // 「助手能操作哪些站点」由用户在界面上登记，不再写死在代码里。
  // 这四个接口是那条验收口径的通道：新增一个站点全程不改 Python。
  listSites: () => json('/api/sites'),

  createSite: (payload) =>
    json('/api/sites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  updateSite: (siteKey, payload) =>
    json(`/api/sites/${encodeURIComponent(siteKey)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  deleteSite: (siteKey) =>
    json(`/api/sites/${encodeURIComponent(siteKey)}`, { method: 'DELETE' }),

  /* ---------- 操作审批闸门（2026-10-15 阶段 C ②） ---------- */
  // 「改变网站状态的点击」必须先在界面上被批准一次，而且只能执行一次。
  // 批准与执行是**两个**请求：把它们合成一个看似更省事，但那样
  // 「点了按钮」就同时等于「批准过」与「执行了」，审计时间线上再也分不清
  // 用户到底确认了什么。分开之后，批准落一条记录，执行落另一条。
  listApprovals: (limit = 20) =>
    json(`/api/approvals?limit=${encodeURIComponent(limit)}`),

  approveApproval: (approvalId) =>
    json(`/api/approvals/${encodeURIComponent(approvalId)}/approve`, { method: 'POST' }),

  rejectApproval: (approvalId) =>
    json(`/api/approvals/${encodeURIComponent(approvalId)}/reject`, { method: 'POST' }),

  executeApproval: (approvalId) =>
    json(`/api/approvals/${encodeURIComponent(approvalId)}/execute`, { method: 'POST' }),

  /* ---------- 审计时间线（2026-10-15 阶段 C ④） ---------- */
  // 「助手做过什么」的证据页：凭证审计 + 操作审计合成的一条倒序流水。
  // 翻页用**游标**而不是 offset —— 时间线持续增长且新事件插在最前，
  // 用 offset 会让「第 1 页的最后一条」滑到第 2 页开头，用户看到重复。
  // 游标由后端在上一次响应里给出，前端只负责原样回传。
  auditTimeline: ({ limit = 30, cursor = '' } = {}) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set('cursor', cursor);
    return json(`/api/audit/timeline?${params.toString()}`);
  },

  /* ---------- 会话 ---------- */
  listSessions: (mode = 'school') =>
    json(`/api/sessions?assistant_mode=${encodeURIComponent(mode)}`),

  clearSessions: (mode = 'school') =>
    json(`/api/sessions?assistant_mode=${encodeURIComponent(mode)}`, { method: 'DELETE' }),

  history: (sessionId, mode = 'school') =>
    json(`/api/chat/${encodeURIComponent(sessionId)}/history?assistant_mode=${encodeURIComponent(mode)}`),

  deleteHistory: (sessionId, mode = 'school') =>
    json(`/api/chat/${encodeURIComponent(sessionId)}/history?assistant_mode=${encodeURIComponent(mode)}`, { method: 'DELETE' }),
};
