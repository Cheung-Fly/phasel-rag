/* ============================================================
   sse.js — SSE 流式客户端
   ------------------------------------------------------------
   为什么不用浏览器原生 EventSource：
     EventSource 只支持 GET，且不能带请求体。
     我们要 POST 一个 JSON（问题 + session_id），所以用
     fetch + ReadableStream 手动解析 SSE 协议。

   SSE 报文格式：
     data: {"type":"token","text":"你"}\n\n
     data: {"type":"token","text":"好"}\n\n
   每条以 "data: " 开头，以「空行」结束。

   ★ 两个必须处理的坑：
     坑1 网络分片任意 —— JSON 可能被切成两半，必须用 buffer 累积，
         只处理「以 \n\n 结尾」的完整消息。
     坑2 汉字占 3 字节 —— 可能被切在中间。必须用
         TextDecoder(..., {stream:true}) 让它暂存不完整字节。
   ============================================================ */
'use strict';

const SSE = (() => {

  /**
   * 发起流式请求。
   *
   * @param {string}   url      接口地址
   * @param {object}   body     请求体（会被 JSON 序列化）
   * @param {function} onEvent  每收到一个事件调用一次
   * @param {AbortSignal} signal 用于取消（停止生成）
   */
  async function post(url, body, onEvent, signal) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,                       // 传下去，abort() 时 fetch 会抛 AbortError
    });

    if (!resp.ok) {
      // 尽量把后端的 detail 读出来，比裸的状态码有用得多
      let detail = '';
      try {
        const j = await resp.json();
        detail = j.detail || JSON.stringify(j);
      } catch {
        detail = await resp.text().catch(() => '');
      }
      throw new Error(`HTTP ${resp.status} ${detail || resp.statusText}`);
    }
    if (!resp.body) throw new Error('响应没有 body，无法流式读取');

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let eventCount = 0;

    // ★ 显式的中止检查。
    //   靠 fetch 自己抛 AbortError 是不够的：如果中止发生在「已经收到部分数据」
    //   之后，reader.read() 往往是**正常 resolve**（done=true）而不是 reject，
    //   于是循环安静地结束，调用方根本不知道是被中止了 —— 会误以为生成完成。
    //   所以这里每轮都检查一次 signal.aborted，确保中止一定以 AbortError 冒泡出去。
    const abortError = () => {
      const e = new Error('Aborted');
      e.name = 'AbortError';
      return e;
    };

    while (true) {
      if (signal?.aborted) throw abortError();

      const { done, value } = await reader.read();
      // stream:true 让解码器把「半个汉字」暂存到下一次
      if (value) buffer += decoder.decode(value, { stream: true });
      if (done) break;

      // 只处理完整的消息，最后一段留回 buffer
      const parts = buffer.split('\n\n');
      buffer = parts.pop();

      for (const part of parts) {
        if (signal?.aborted) throw abortError();
        const line = part.split('\n').find((l) => l.startsWith('data:'));
        if (!line) continue;
        const json = line.slice(5).trim();
        if (!json) continue;
        try {
          onEvent(JSON.parse(json));
          eventCount++;
        } catch {
          console.warn('[SSE] 无法解析的片段:', json);
        }
      }
    }

    // 收尾：把解码器里可能残留的字节吐出来
    buffer += decoder.decode();
    if (buffer.trim()) {
      const line = buffer.split('\n').find((l) => l.startsWith('data:'));
      if (line) {
        try { onEvent(JSON.parse(line.slice(5).trim())); eventCount++; } catch {}
      }
    }

    return { eventCount };
  }

  return { post };
})();
