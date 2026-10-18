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
import { setBrowserHandoffActive } from './useBrowserHandoff.js';
import { useAssistantMode } from './useAssistantMode.js';
import { useAuditTimeline } from './useAuditTimeline.js';
import { useApprovals } from './useApprovals.js';
import * as storage from '../lib/storage.js';

/* 消息形状：
     { id, role:'user'|'bot', text, error, streaming, stopped,
       question, sources, meta, pendingApproval }
   question 是「这条回答对应哪个提问」，供「重新生成」使用；
   它是可空的 —— 欢迎语这类机器人消息没有对应提问。
   pendingApproval 是个人助手里「等待批准的一次页面点击」，形如
   { approval_id, target, status, expires_at }；非空时消息下方会渲染一张
   「批准并执行 / 拒绝」卡片（见 ClickApprovalCard.vue）。
   ★ 阶段 C ② 起这里放的不是确认文本而是 approval_id：批准必须对应
     数据库里一条能验证、能审计、且只能消费一次的记录。 */

let seq = 0;
const nextId = () => `m${++seq}`;

const messages = ref([]);
const busy = ref(false);
const assistantMode = useAssistantMode();

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
    pendingApproval: null,
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
  const { refresh: refreshAuditTimeline } = useAuditTimeline();
  const { refresh: refreshApprovals } = useApprovals();

  /* ---------------------------------------------------------- 发送 */
  async function send(text, opts = {}) {
    if (busy.value) return;

    const question = String(text || '').trim();
    if (!question) return;
    const mode = assistantMode.value;

    busy.value = true;
    // 一旦有新的一轮对话开始，之前那些「待批准的操作」就已经失去意义
    // （后端 90 秒 TTL 且一次有效）。不清掉的话，用户会对着一条早就过期的
    // 记录点下去，收到一个 410 —— 而界面上看起来什么都没发生。
    // ★ 已经落定的（已执行 / 已拒绝 / 已超时 / 失败）要保留：那张卡片上的
    //   结果说明是这个操作唯一的可见交代，清掉它等于「做完了但没人告诉你」。
    messages.value.forEach((m) => {
      if (m.pendingApproval && !m.pendingApproval.settled) m.pendingApproval = null;
    });
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
        {
          question,
          session_id: currentId.value || '',
          assistant_mode: mode,
        },
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

            case 'browser_session':
              setBrowserHandoffActive(Boolean(ev.active));
              break;

            case 'click_approval':
              // 后端已经在 PG 里登记了一条 pending 审批，并把它下发过来；
              // 渲染成卡片，批准/执行分别调 /api/approvals/{id}/approve 与 /execute。
              bot.pendingApproval = ev.approval || null;
              break;

            case 'shell_approval':
              // 终端命令审批（run_shell）：与网页点击同一套闸门、同一张卡片，
              // 只是卡片内容是「命令 + 人话翻译」、执行后回显 stdout。
              // 后端事件名刻意区分（shell_approval / click_approval），
              // 但前端载体刻意共用 —— 两种审批对用户的交互语义完全一致。
              bot.pendingApproval = ev.approval || null;
              break;

            case 'analysis_approval':
              // 数据分析脚本审批（data_analysis）：同样共用审批卡片，
              // 卡片展示「分析目标 + 脚本摘要」，批准后执行、回显统计结论。
              // 与 shell_approval 同构 —— 都是「先批准才执行」的写操作。
              bot.pendingApproval = ev.approval || null;
              break;

            case 'mcp_approval':
              // MCP 变更类工具审批：同一张卡片，批准后回显工具返回内容。
              bot.pendingApproval = ev.approval || null;
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

  /* ---------------------------------------------------------- 审批落定 */
  /**
   * 把一条待批准的操作落成终态（由 ClickApprovalCard 上报）。
   *
   * 为什么把结果留在消息上而不是另发一条助手消息：这类结果不是「对话」，
   * 而是「那次操作怎么了」。挂在原来那条消息下面，因果一目了然；
   * 而且它本来就不在服务端的会话记忆里（执行走的是另一个接口），
   * 另发一条反而会在重新载入会话时凭空消失。
   */
  function settleApproval({ index, outcome, message, detail }) {
    const msg = messages.value[index];
    if (!msg || !msg.pendingApproval || msg.pendingApproval.settled) return;
    // detail 是终端命令执行后的 stdout/exit_code 回写：合并进 pendingApproval，
    // 让 ClickApprovalCard 能立即回显「命令输出了什么」，而不是点完就消失。
    const next = { ...msg.pendingApproval, settled: true, outcome, message };
    if (detail) next.detail = { ...(msg.pendingApproval.detail || {}), ...detail };
    msg.pendingApproval = next;
    // 卡片落了终态就等于后端刚写下一条审计事件，顺手把时间线重取一次 ——
    // 于是「我批准了什么」与「它记下了什么」在屏幕上同时出现，可以互相对照。
    // 只是重取服务端数据，**不是**在本地往列表里插一行（那会打乱游标分页）。
    refreshAuditTimeline();
    // 侧栏「待批准操作」面板也要跟着刷：消息卡片落定后，面板里那条
    // 若还挂在「待批准」上就是假象。这里和审计一样是重取服务端，不是本地改。
    refreshApprovals();
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
    push(
      'bot',
      assistantMode.value === 'school'
        ? '已开启新的“了解学校”会话。回答只参考知识库内容。'
        : '已开启新的“个人助手”会话。你可以让我登录平台、查看网页或导航；点击可能改变网站状态的控件前，我会先请你确认。',
    );
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
    push(
      'bot',
      assistantMode.value === 'school'
        ? '你好，这里是“了解学校”。我会依据知识库资料回答校园相关问题；资料不足时会明确说明。'
        : '你好，这里是“个人助手”。登录并确认人工验证后，我可以查看网页、打开安全的同站链接，也可以按你的指示点击页面控件；可能改变网站状态的点击会先请你逐次确认。',
    );
  }

  async function activateMode(mode) {
    setCurrent(storage.getCurrent(mode));
    messages.value = [];
    await sync(mode);
    if (currentId.value) {
      const loaded = await openSession(currentId.value);
      if (loaded) return;
    }
    showWelcome();
  }

  /* ---------------------------------------------------------- 打开历史会话 */
  async function openSession(id) {
    if (busy.value) { toast('正在生成中，请先停止', 'error'); return false; }

    try {
      const d = await api.history(id, assistantMode.value);
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
    try { await api.deleteHistory(id, assistantMode.value); } catch { /* 忽略 */ }

    removeLocal(id);
    if (id === currentId.value) newChat();
    await sync();
    toast('会话已删除');
  }

  return {
    messages, busy, scrollReq,
    send, stop, regenerate, newChat, showWelcome, openSession, deleteSession,
    activateMode, settleApproval,
  };
}
