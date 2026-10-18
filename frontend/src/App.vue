<script setup>
/* ============================================================
   App —— 布局外壳 + 应用级初始化
   ------------------------------------------------------------
   组件树：
     App
      ├─ AppSidebar（品牌 / 上传 / 文档 / 会话 / 健康）
      ├─ ChatPanel（头部 / 消息区 / 输入区）
      └─ ToastBar（全局提示）

   为什么初始化放在本组件的 onMounted、而不是 main.js：
     初始化里要做「恢复上次会话」「聚焦输入框」「滚动到最新」这类
     依赖真实 DOM 的事。main.js 里此时组件还没挂载，
     DOM 引用全是 null。放到 onMounted 才保证节点已就位。

   为什么快捷键在这里监听（而不是各组件里）：
     它们作用域是整个页面，且会跨组件（Ctrl+K 是给别人发信号、
     Esc 是去中断 useChat 的请求）。挂在 window 上一处处理，
     比分散到三个组件里更容易看清「到底绑了哪些键」。
   ============================================================ */
import { onMounted, onUnmounted, ref } from 'vue';
import AppSidebar from './components/AppSidebar.vue';
import ChatPanel from './components/ChatPanel.vue';
import ToastBar from './components/ToastBar.vue';
import BrowserHandoff from './components/BrowserHandoff.vue';

import { useTheme } from './composables/useTheme.js';
import { useHealth } from './composables/useHealth.js';
import { useDocuments } from './composables/useDocuments.js';
import { useSites } from './composables/useSites.js';
import { useAuditTimeline } from './composables/useAuditTimeline.js';
import { useSessions } from './composables/useSessions.js';
import { useChat } from './composables/useChat.js';
import { useComposer } from './composables/useComposer.js';
import { getCurrent, setUserScope } from './lib/storage.js';
import { api } from './lib/api.js';
import { useAssistantMode, setAssistantMode } from './composables/useAssistantMode.js';
import {
  setBrowserHandoffActive,
  setBrowserHandoffAvailable,
} from './composables/useBrowserHandoff.js';

// 窄屏抽屉的开关状态。只在 App 层持有，向下传给侧栏。
const sidebarOpen = ref(false);
// 启动引导三态：进行中 / 完成 / 失败。
// ★ 2026-10-14：单用户本地模式后这里不再有「未登录」这一态 ——
//   后端不再要求会话，前端也就不该再保留任何登录态判断。
const booted = ref(false);
const bootError = ref('');
// 运行期提示（例如浏览器验证会话恢复失败）。它承载的是「服务连上了，
// 但某个子功能有问题」这类非致命信息，跟启动失败不是一回事，因此分开。
const appError = ref('');

const { apply: applyTheme, toggle: toggleTheme } = useTheme();
const { start: startHealth, stop: stopHealth } = useHealth();
const { refresh: refreshDocs } = useDocuments();
const { refresh: refreshSites } = useSites();
const { refresh: refreshAudit } = useAuditTimeline();
const { sync, setCurrent } = useSessions();
const {
  busy,
  stop: stopGenerating,
  openSession,
  showWelcome,
  activateMode,
} = useChat();
const { requestFocus } = useComposer();
const assistantMode = useAssistantMode();

async function restoreBrowserSession() {
  try {
    const browserSession = await api.browserSession();
    const sessionAvailable = ['verification_pending', 'login_confirmed']
      .includes(browserSession.status);
    setBrowserHandoffAvailable(sessionAvailable);
    setBrowserHandoffActive(sessionAvailable);
  } catch (error) {
    setBrowserHandoffAvailable(false);
    appError.value = `无法恢复验证浏览器会话：${error.message || '服务暂不可用'}`;
  }
}

function onKeydown(e) {
  // Ctrl/Cmd + K：聚焦输入框
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    requestFocus();
    return;
  }

  // Ctrl/Cmd + /：切换主题
  if ((e.ctrlKey || e.metaKey) && e.key === '/') {
    e.preventDefault();
    toggleTheme();
    return;
  }

  // Esc：停止生成。只在真的在生成时才拦，
  // 否则会吃掉其它（将来可能有的）Esc 语义。
  if (e.key === 'Escape' && busy.value) {
    stopGenerating();
  }
}

async function initializeApplication() {
  // 三件事并行发起没有意义 —— 都很快，但顺序影响观感：
  // 先把文档列表和健康状态铺好（用户一进来就能看到「有哪些文档」），
  // 再去决定对话区要显示历史还是欢迎语。
  await refreshDocs();
  // 站点列表不 await：它的结果不影响下面「显示历史还是欢迎语」的判断，
  // 而它是个人助手侧才显示的面板 —— 让它挡住对话区的出现不值得。
  // refresh() 内部已经吞掉了失败（转成面板上的 listError），不会产生未捕获的 rejection。
  refreshSites();
  startHealth();

  const list = await sync();

  // 恢复上次的会话。两个条件都要满足：
  //   1) 本地记着一个 session_id；
  //   2) 它确实还在服务器返回的列表里 —— 否则会去请求一个已删除的会话，
  //      用户看到的是空的对话区，会以为数据丢了。
  const saved = getCurrent(assistantMode.value);
  if (saved && list.some((s) => s.id === saved)) {
    await openSession(saved);
  } else {
    showWelcome();
  }

  if (assistantMode.value === 'student') {
    // 审计时间线同样不 await：它是个人助手侧的侧栏面板，
    // 没有理由挡住对话区的出现。refresh() 内部已吞掉失败（转成 error ref）。
    refreshAudit();
    await restoreBrowserSession();
  }

  requestFocus();
}

async function switchAssistantMode(mode) {
  if (busy.value || mode === assistantMode.value) return;
  setAssistantMode(mode);
  await activateMode(mode);
  if (mode === 'student') {
    // 切回个人助手时重取一次：时间线里的一条条都是「刚刚那次操作到底
    // 成没成」，用户切过来的时机往往正是想确认这件事。
    refreshAudit();
    await restoreBrowserSession();
  }
  requestFocus();
}

async function bootstrap() {
  // 单用户本地模式下的「启动」只有一件事：取回本机主人身份。
  // ★ 为什么仍然要问一次 /api/auth/me，而不是干脆不要这个请求：
  //   身份里的 user_id 是 localStorage 命名空间的一部分
  //   （rag.<user_id>.sessions / .currentSession，见 lib/storage.js）。
  //   跳过它就等于让本地索引换一个命名空间，用户会看到「历史会话全没了」。
  //   所以这里保留这次调用，但它的失败语义变了：不是「去登录」，
  //   而是「后端没起来」—— 因此展示的是一个可重试的错误页。
  bootError.value = '';
  try {
    const user = await api.authMe();
    api.health().then((h) => { canLogout.value = h.auth_mode === 'password'; }).catch(() => {});
    setUserScope(user.user_id);
    setCurrent(null);
    await initializeApplication();
  } catch (error) {
    setBrowserHandoffAvailable(false);
    if (error.status === 401) {
      needsLogin.value = true;
      return;
    }
    bootError.value = error.message || '无法连接本机助手服务';
  } finally {
    booted.value = true;
  }
}

const needsLogin = ref(false);
const canLogout = ref(false);
const loginPassword = ref('');
const loginError = ref('');
const loginBusy = ref(false);

async function submitLogin() {
  if (!loginPassword.value || loginBusy.value) return;
  loginBusy.value = true;
  loginError.value = '';
  try {
    await api.authLogin(loginPassword.value);
    loginPassword.value = '';
    needsLogin.value = false;
    await retryBoot();
  } catch (error) {
    loginError.value = error.message || '登录失败';
  } finally {
    loginBusy.value = false;
  }
}

function onAuthExpired() {
  needsLogin.value = true;
  loginError.value = '登录已过期，请重新输入口令';
}

async function logout() {
  try {
    await api.authLogout();
  } finally {
    window.location.reload();
  }
}

async function retryBoot() {
  booted.value = false;
  await bootstrap();
}

onMounted(async () => {
  applyTheme();
  window.addEventListener('keydown', onKeydown);
  window.addEventListener('auth-expired', onAuthExpired);
  await bootstrap();
});

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown);
  window.removeEventListener('auth-expired', onAuthExpired);
  // 停止轮询，避免组件销毁后定时器还在打接口（开发期热更新时尤其明显）
  stopHealth();
});
</script>

<template>
  <!-- 启动引导。原来是「正在验证登录状态 → 登录表单 → 主界面」三步，
       单用户本地模式后收敛为「启动中 → 主界面」，只在启动失败时多一屏。 -->
  <main v-if="!booted" class="auth-gate">
    <p>正在启动本机助手…</p>
  </main>
  <main v-else-if="needsLogin" class="auth-gate">
    <form class="auth-card" @submit.prevent="submitLogin">
      <h1>请输入访问口令</h1>
      <input v-model="loginPassword" type="password" autocomplete="current-password" autofocus placeholder="访问口令" />
      <p v-if="loginError" class="auth-error" role="alert">{{ loginError }}</p>
      <button type="submit" :disabled="loginBusy || !loginPassword">登录</button>
    </form>
  </main>
  <main v-else-if="bootError" class="auth-gate">
    <div class="auth-card">
      <h1>无法连接到本机助手</h1>
      <p>{{ bootError }}</p>
      <p>页面已加载，但读不到本机后端。请确认后端服务已启动后重试。</p>
      <button type="button" @click="retryBoot">重试</button>
    </div>
  </main>
  <template v-else>
    <button class="btn-menu" title="显示/隐藏侧栏" @click="sidebarOpen = !sidebarOpen">☰</button>
    <button v-if="canLogout" class="btn-logout" title="退出登录" @click="logout">退出</button>
    <div class="layout">
      <AppSidebar :open="sidebarOpen" :assistant-mode="assistantMode" />
      <ChatPanel
        :assistant-mode="assistantMode"
        :busy="busy"
        @switch-mode="switchAssistantMode"
        @close-sidebar="sidebarOpen = false"
      />
    </div>
    <p v-if="appError" class="auth-toast" role="alert">{{ appError }}</p>
  </template>
  <ToastBar />
  <BrowserHandoff v-if="assistantMode === 'student'" />
</template>

<style scoped>
/* .auth-gate / .auth-card 现在只服务「启动中」与「启动失败」两屏。
   登录表单相关的 label / input / :disabled 规则已随登录界面一并删除，
   留着它们只会让人以为这里还有一条登录通路。 */
.auth-gate {
  display: grid;
  min-height: 100vh;
  place-items: center;
  padding: 20px;
}
.auth-card {
  display: grid;
  gap: 14px;
  width: min(400px, 100%);
  padding: 24px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  box-shadow: var(--shadow);
}
.auth-card h1, .auth-card p { margin: 0; }
.auth-card h1 { font-size: 20px; }
.auth-card p { color: var(--text-dim); }
.auth-card button {
  padding: 10px;
  border: 0;
  border-radius: var(--radius-sm);
  background: var(--accent);
  color: white;
  cursor: pointer;
}
.auth-error, .auth-toast { color: var(--err) !important; }
.auth-toast {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 10;
  padding: 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-panel);
}
</style>
