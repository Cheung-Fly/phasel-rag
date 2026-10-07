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

const json = (url, init) => fetch(url, init).then(asJson);

export const api = {
  /* ---------- 基础 ---------- */
  health: () => json('/health'),

  /* ---------- 文档 ---------- */
  listDocuments: () => json('/api/documents'),

  deleteDocument: (docId) =>
    json(`/api/documents/${encodeURIComponent(docId)}`, { method: 'DELETE' }),

  /**
   * 上传文档。
   * ★ 这里坚持用 XMLHttpRequest 而不是 fetch：fetch 至今没有「上传进度」事件，
   *   而入库一份文档要调上百次 embedding、耗时可能几十秒到几分钟，
   *   没有进度条用户会以为卡死。fetch 只有下行的 ReadableStream 进度，
   *   上行进度在 Web 平台只有 XHR 提供。
   *
   * 注意：进度只反映「文件字节传完」，之后后端还要解析 + 切片 + 向量化，
   * 那段时间进度条会停在 100% —— 这是诚实的表现，所以文案写成
   * 「上传中…」而非「入库中…」，入库阶段由调用方单独提示。
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

  /* ---------- 会话 ---------- */
  listSessions: () => json('/api/sessions'),

  clearSessions: () => json('/api/sessions', { method: 'DELETE' }),

  history: (sessionId) =>
    json(`/api/chat/${encodeURIComponent(sessionId)}/history`),

  deleteHistory: (sessionId) =>
    json(`/api/chat/${encodeURIComponent(sessionId)}/history`, { method: 'DELETE' }),
};
