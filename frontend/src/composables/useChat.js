/* ============================================================
   useChat —— 消息列表 + 流式问答 + 会话打开/删除
   ------------------------------------------------------------
   这是整个前端最核心的一块，迁移时逐行对齐了原 app.js 的行为，
   下面几处都是「踩过坑才写对」的地方，不要凭直觉简化：

   ★ 坑 1：Vue 响应式代理
     往 ref([]) 里 push 一个普通对象后，直接改那个对象**不会触发更新** ——
     Vue 存进去的是原始对象，只有通过代理读写才会被追踪。
     所以 push() 之后必须再从数组里读一次（读出来的是代理），
     后续的流式追加都写在这个代理上。

   ★ 坑 2：流式渲染必须节流
     一次回答可能有上百个 token，每个都重跑一遍 Markdown 解析会明显卡顿。
     这里用 requestAnimationFrame 累积到下一帧再一次性写入 ——
     每帧最多渲染一次。

   ★ 坑 3：中止要走完收尾
     用户点「停止生成」时，已经收到的内容必须 flush 出来并保留
     （符合直觉：我看到的就该留在屏幕上），而 rAF 里还没落盘的那部分
     若不显式 flush 会永远丢失。
   ============================================================ */
import { ref } from 'vue';
import { api } from '../lib/api.js';
import { postStream } from '../lib/sse.js';
import { useToast } from './useToast.js';
import { useSessions } from './useSessions.js';
import { useComposer } from './useComposer.js';

/* 消息形状：
     { id, role:'user'|'bot', text, error, streaming, stopped,
       question, sources, meta }
   question 是「这条回答对应哪个提问」，供「重新生成」使用；
   它是可空的 —— 欢迎语这类机器人消息没有对应提问。 */

let seq = 0;
const nextId = () => `m${++seq}`;

const messages = ref([]);
const busy = ref(false);

/**
 * 滚动请求信号。
 * 数据层不该直接操作 DOM，所以这里只发一个「请滚一下」的信号，
 * 由 MessageList 组件监听并决定滚不滚（是否已接近底部）。
 * force=true 表示「新消息进来了，无条件滚到底」。
 */
const scrollReq = ref({ tick: 0, force: false });

let controller = null;   // AbortController，用于「停止生成」

function requestScroll(force = false) {
  scrollReq.value = { tick: scrollReq.value.tick + 1, force };
}

/**
 * 追加一条消息，返回它的**响应式代理**（见文件头「坑 1」）。
 */
function push(role, text, opts = {}) {
  messages.value.push({
    id: nextId(),
    role,
    text,
    error: false,
    streaming: false,
    stopped: false,
    question: null,
    sources: null,
    meta: null,
    ...opts,
  });
  const proxy = messages.value[messages.value.length - 1];
  requestScroll(true);
  return proxy;
}

export function useChat() {
  const { toast } = useToast();
  const { currentId, upsert, setCurrent, removeLocal, sync } = useSessions();
  const { requestFocus } = useComposer();

  /* ---------------------------------------------------------- 发送 */
  async function send(text, opts = {}) {
    if (busy.value) return;

    const question = String(text || '').trim();
    if (!question) return;

    busy.value = true;
    if (!opts.skipUserBubble) push('user', question);

    const bot = push('bot', '', { streaming: true, question });
    controller = new AbortController();

    let sources = [];
    let chunks = 0;
    let chars = 0;
    const t0 = performance.now();
    let tFirst = null;

    /* ---- rAF 节流（见文件头「坑 2」） ---- */
    let pending = '';
    let raf = null;

    const flush = () => {
      if (raf) { cancelAnimationFrame(raf); raf = null; }
      if (pending) { bot.text += pending; pending = ''; }
    };

    const appendToken = (t) => {
      pending += t;
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = null;
        bot.text += pending;
        pending = '';
        requestScroll();
      });
    };

    try {
      await postStream(
        '/api/chat/stream',
        { question, session_id: currentId.value || '' },
        (ev) => {
          switch (ev.type) {
            case 'meta':
              // 后端会把新建会话的 id 回传；与本地不一致说明这是本次新建的
              if (ev.session_id && ev.session_id !== currentId.value) {
                setCurrent(ev.session_id);
              }
              break;

            case 'sources':
              sources = ev.sources || [];
              break;

            case 'token':
              if (tFirst === null) tFirst = performance.now();
              chunks++;
              chars += (ev.text || '').length;
              appendToken(ev.text);
              break;

            case 'done':
              break;

            case 'error':
              flush();
              bot.streaming = false;
              bot.error = true;
              bot.text = '生成出错：' + ev.message;
              break;
          }
        },
        controller.signal,
      );

      flush();

      const total = performance.now() - t0;
      const ttft = tFirst ? tFirst - t0 : total;

      bot.streaming = false;
      bot.sources = sources.length ? sources : null;
      bot.meta = `${(ttft / 1000).toFixed(2)}s 首字 · ` +
                 `${(total / 1000).toFixed(2)}s 总 · ` +
                 `${chunks} 块 · ${chars} 字`;
      requestScroll();

      // 会话索引：用第一个问题当标题（reconcile 时会以服务器的为准）
      upsert(currentId.value, question.slice(0, 30), Date.now());

    } catch (e) {
      flush();
      bot.streaming = false;

      if (e.name === 'AbortError') {
        // 用户点了「停止生成」：保留已生成的部分，这是符合直觉的（见坑 3）
        bot.stopped = true;
        bot.meta = '已停止';
        if (bot.text) upsert(currentId.value, question.slice(0, 30), Date.now());
        toast('已停止生成');
      } else {
        bot.error = true;
        bot.text = '请求失败：' + e.message;
        toast('请求失败：' + e.message, 'error');
      }
      requestScroll();

    } finally {
      busy.value = false;
      controller = null;
      // 一轮结束后把焦点还给输入框：连续追问是这里的常态，
      // 每次都要用鼠标点回去非常打断节奏。
      requestFocus();
    }
  }

  /* ---------------------------------------------------------- 停止 */
  function stop() {
    if (controller) controller.abort();
  }

  /* ---------------------------------------------------------- 重新生成 */
  /**
   * @param {number} index 要重新生成的助手消息下标
   * 语义：删掉这条及其之后的所有消息，再用同一个问题重问。
   * 为什么删掉后面的：那些回答都是建立在「这条存在」的前提上，
   * 留着会让上下文错乱。
   */
  function regenerate(index) {
    if (busy.value) { toast('正在生成中，请先停止', 'error'); return; }

    const msg = messages.value[index];
    if (!msg || !msg.question) { toast('无法重新生成', 'error'); return; }

    const question = msg.question;
    messages.value.splice(index);       // 删掉这条及其后所有消息
    send(question, { skipUserBubble: false });
  }

  /* ---------------------------------------------------------- 新会话 */
  function newChat() {
    setCurrent(null);
    messages.value = [];
    push('bot', '已开启新会话。\n\n之前的对话记忆不再参与，但**知识库文档仍然可用**。');
    requestFocus();
  }

  /**
   * 首次进入、且本地没有可恢复的会话时展示的引导语。
   * 与 newChat 的区别：newChat 是用户主动点的（知道自己在做什么），
   * 这里是第一次来（需要知道接下来该干什么），所以文案不同。
   */
  function showWelcome() {
    setCurrent(null);
    messages.value = [];
    push('bot',
      '你好，我是基于你上传文档回答问题的助手。\n\n' +
      '1. 先在左侧**上传一份文档**（支持 .md / .txt / .pdf）\n' +
      '2. 然后在下面提问\n\n' +
      '我会尽量只依据文档内容回答；找不到就明说，**不会编造**。');
  }

  /* ---------------------------------------------------------- 打开历史会话 */
  async function openSession(id) {
    if (busy.value) { toast('正在生成中，请先停止', 'error'); return false; }

    try {
      const d = await api.history(id);
      const history = d.history || [];

      setCurrent(id);
      messages.value = [];

      if (!history.length) {
        // 本地索引里有、服务器却没有 —— 通常是对不上，明确告诉用户而不是静默
        toast('这段会话的记忆已丢失（容器可能重启过）', 'error');
        return false;
      }

      for (const h of history) {
        push('user', h.user);
        push('bot', h.ai, { question: h.user });
      }
      requestScroll(true);
      toast(`已载入 ${history.length} 轮对话`);
      return true;
    } catch (e) {
      toast('载入失败：' + e.message, 'error');
      return false;
    }
  }

  /* ---------------------------------------------------------- 删除会话 */
  async function deleteSession(id) {
    if (!confirm('删除这段会话？只影响对话记忆，不影响知识库文档。')) return;

    // 服务器删除失败也要继续清本地：否则会留下一段「看得见点不开」的僵尸条目
    try { await api.deleteHistory(id); } catch { /* 忽略 */ }

    removeLocal(id);
    if (id === currentId.value) newChat();
    await sync();
    toast('会话已删除');
  }

  return {
    messages, busy, scrollReq,
    send, stop, regenerate, newChat, showWelcome, openSession, deleteSession,
  };
}
