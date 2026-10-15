<script setup>
import { onUnmounted, ref, watch } from 'vue';
import { api } from '../lib/api.js';
import {
  setBrowserHandoffAvailable,
  useBrowserHandoff,
} from '../composables/useBrowserHandoff.js';

const { active, available } = useBrowserHandoff();
const image = ref(null);
const status = ref('verification_pending');
const expiresIn = ref(0);
const busy = ref(false);
const error = ref('');
const frame = ref(null);
const viewportWidth = ref(window.innerWidth);
const panelWidth = ref(Math.round(window.innerWidth / 2));

let timer = null;
let pointerDown = false;
let resizePointerId = null;
const actionQueue = [];
let actionInFlight = false;
let pendingMove = null;
let moveTimer = null;

function coordinates(event) {
  const bounds = image.value.getBoundingClientRect();
  return {
    x: (event.clientX - bounds.left) * image.value.naturalWidth / bounds.width,
    y: (event.clientY - bounds.top) * image.value.naturalHeight / bounds.height,
  };
}

function startResize(event) {
  event.preventDefault();
  resizePointerId = event.pointerId;
  event.currentTarget.setPointerCapture(event.pointerId);
  resizePanel(event);
}

function resizePanel(event) {
  if (resizePointerId !== event.pointerId) return;
  const minWidth = Math.min(320, viewportWidth.value);
  panelWidth.value = Math.max(
    minWidth,
    Math.min(Math.round(viewportWidth.value * 0.85), viewportWidth.value - event.clientX),
  );
}

function finishResize(event) {
  if (resizePointerId === event.pointerId) resizePointerId = null;
}

function resizeWithKeyboard(event) {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
  event.preventDefault();
  const adjustment = event.key === 'ArrowLeft' ? 24 : -24;
  const minWidth = Math.min(320, viewportWidth.value);
  panelWidth.value = Math.max(
    minWidth,
    Math.min(Math.round(viewportWidth.value * 0.85), panelWidth.value + adjustment),
  );
}

function onViewportResize() {
  viewportWidth.value = window.innerWidth;
  panelWidth.value = Math.min(panelWidth.value, viewportWidth.value);
}

function syncDockSpace() {
  const docked = active.value && status.value === 'login_confirmed';
  document.documentElement.style.setProperty(
    '--browser-dock-width',
    docked ? `${Math.min(panelWidth.value, viewportWidth.value)}px` : '0px',
  );
}

async function refresh() {
  if (!active.value || busy.value || pointerDown || actionInFlight || actionQueue.length) return;
  try {
    const snapshot = await api.browserSession();
    status.value = snapshot.status;
    expiresIn.value = snapshot.expires_in || 0;
    image.value && (image.value.src = snapshot.image
      ? `data:image/jpeg;base64,${snapshot.image}`
      : '');
    error.value = '';
    if (snapshot.status === 'session_expired' || snapshot.status === 'session_closed') {
      setBrowserHandoffAvailable(false);
      image.value && (image.value.src = '');
    }
  } catch (exception) {
    error.value = exception.message || '无法读取验证页面';
  }
}

async function sendAction(action, event) {
  if (!active.value) return;
  const point = event && image.value && Number.isFinite(event.clientX)
    ? coordinates(event)
    : {};
  if (event?.key) point.key = event.key;

  const previous = action === 'move' && actionQueue.at(-1)?.action === 'move'
    ? actionQueue.at(-1)
    : null;
  return new Promise((resolve) => {
    if (previous) {
      previous.point = point;
      previous.waiters.push(resolve);
    } else {
      actionQueue.push({ action, point, waiters: [resolve] });
    }
    processActions();
  });
}

async function processActions() {
  if (actionInFlight) return;
  actionInFlight = true;
  try {
    while (actionQueue.length) {
      const item = actionQueue.shift();
      try {
        await api.browserSessionAction(item.action, item.point);
      } catch (exception) {
        error.value = exception.message || '浏览器输入失败';
      } finally {
        item.waiters.forEach((resolve) => resolve());
      }
    }
  } finally {
    actionInFlight = false;
  }
}

async function onPointerDown(event) {
  event.preventDefault();
  pointerDown = true;
  event.currentTarget.setPointerCapture(event.pointerId);
  frame.value?.focus();
  await sendAction('down', event);
}

function onPointerMove(event) {
  if (!pointerDown) return;
  pendingMove = event;
  if (!moveTimer) {
    moveTimer = setTimeout(() => {
      moveTimer = null;
      const latest = pendingMove;
      pendingMove = null;
      if (latest) sendAction('move', latest);
    }, 60);
  }
}

async function onPointerUp(event) {
  if (!pointerDown) return;
  pointerDown = false;
  clearTimeout(moveTimer);
  moveTimer = null;
  if (pendingMove) {
    await sendAction('move', pendingMove);
    pendingMove = null;
  }
  await sendAction('up', event);
  refresh();
}

function onKeydown(event) {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const key = event.key.length === 1 || [
    'Enter', 'Tab', 'Backspace', 'Delete',
    'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
  ].includes(event.key) ? event.key : '';
  if (!key) return;
  event.preventDefault();
  sendAction('press', { key });
}

async function confirm() {
  busy.value = true;
  try {
    const result = await api.completeBrowserVerification();
    status.value = result.status;
    error.value = result.status === 'verification_pending'
      ? '页面仍在等待登录或验证码提交，请在上方页面完成后再确认。'
      : '';
  } catch (exception) {
    error.value = exception.message || '无法确认登录状态';
  } finally {
    busy.value = false;
  }
}

async function closeSession() {
  busy.value = true;
  try {
    await api.closeBrowserSession();
    setBrowserHandoffAvailable(false);
    image.value && (image.value.src = '');
  } catch (exception) {
    error.value = exception.message || '关闭浏览器会话失败';
  } finally {
    busy.value = false;
  }
}

watch([active, status, panelWidth, viewportWidth], ([enabled]) => {
  syncDockSpace();
  clearInterval(timer);
  if (!enabled) return;
  refresh();
  timer = setInterval(refresh, 1500);
}, { immediate: true });

onUnmounted(() => {
  document.documentElement.style.setProperty('--browser-dock-width', '0px');
  clearInterval(timer);
  clearTimeout(moveTimer);
  window.removeEventListener('resize', onViewportResize);
});

window.addEventListener('resize', onViewportResize);
</script>

<template>
  <button
    v-if="available && !active"
    type="button"
    class="handoff-reopen"
    @click="active = true"
  >
    显示校园网页
  </button>
  <div
    v-if="active"
    class="handoff-backdrop"
    :class="{ 'handoff-docked': status === 'login_confirmed' }"
  >
    <div
      v-if="status === 'login_confirmed'"
      class="handoff-resize-handle"
      role="separator"
      aria-label="调整浏览器面板宽度"
      aria-orientation="vertical"
      :aria-valuenow="Math.round(panelWidth / viewportWidth * 100)"
      aria-valuemin="32"
      aria-valuemax="85"
      tabindex="0"
      :style="{ left: `${viewportWidth - panelWidth}px` }"
      @pointerdown="startResize"
      @pointermove="resizePanel"
      @pointerup="finishResize"
      @pointercancel="finishResize"
      @keydown="resizeWithKeyboard"
    ></div>
    <section
      ref="frame"
      class="handoff-panel"
      :class="{ 'handoff-panel-docked': status === 'login_confirmed' }"
      :style="status === 'login_confirmed' ? { width: `${panelWidth}px` } : undefined"
      tabindex="0"
      :aria-label="status === 'login_confirmed' ? '校园网站浏览器面板' : '校园网站人工验证窗口'"
      @keydown="onKeydown"
    >
      <header>
        <div>
          <!-- ★ 这里原本写死「东软智慧教育平台」。阶段 C 起站点由用户登记，
               再写死一个具体名字就会在别的站点上显示错误信息。站点的真实
               名称由上面那条聊天消息给出（它由站点规格生成），这里保持中性。 -->
          <strong>校园网站{{ status === 'login_confirmed' ? ' · 已登录' : ' · 人工验证' }}</strong>
          <p>
            {{ status === 'login_confirmed'
              ? '浏览器会话保持开启；可拖动左侧边缘调整面板宽度。'
              : '账号和密码已自动填入，请检查下方页面；验证码由你本人完成并点击登录。助手不会代答验证码。' }}
          </p>
        </div>
        <button type="button" class="btn-ghost" :disabled="busy" @click="closeSession">
          关闭并清除会话
        </button>
      </header>
      <div class="handoff-status" role="status">
        {{ status === 'login_confirmed' ? '你已确认登录完成' : `请完成人工验证 · 空闲超时 ${Math.ceil(expiresIn / 60)} 分钟` }}
      </div>
      <div class="handoff-screen">
        <img
          ref="image"
          alt="远程登录页面。点击、拖动或在此窗口聚焦后使用键盘完成验证"
          @pointerdown="onPointerDown"
          @pointermove="onPointerMove"
          @pointerup="onPointerUp"
          @pointercancel="onPointerUp"
        >
        <span v-if="!image?.src" class="handoff-loading">正在加载验证页面…</span>
      </div>
      <p v-if="error" class="handoff-error" role="alert">{{ error }}</p>
      <footer>
        <span>
          {{ status === 'login_confirmed'
            ? `浏览器会话保持开启 · 空闲 ${Math.ceil(expiresIn / 60)} 分钟后过期`
            : '点击页面可输入；拖动可完成滑块操作。15 分钟无活动后会清除会话。' }}
        </span>
        <button v-if="status !== 'login_confirmed'" type="button" :disabled="busy" @click="confirm">
          {{ busy ? '处理中…' : '我已完成验证' }}
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.handoff-backdrop {
  position: fixed;
  inset: 0;
  z-index: 30;
  display: grid;
  place-items: center;
  padding: 18px;
  background: rgb(0 0 0 / 65%);
}
.handoff-backdrop.handoff-docked {
  display: flex;
  justify-content: flex-end;
  padding: 0;
  background: transparent;
  pointer-events: none;
}
.handoff-reopen {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 29;
  padding: 10px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-panel);
  color: var(--text);
  box-shadow: var(--shadow);
  cursor: pointer;
}
.handoff-panel {
  display: grid;
  gap: 12px;
  width: min(1000px, 100%);
  max-height: 94vh;
  padding: 16px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  box-shadow: var(--shadow);
  outline: none;
}
.handoff-panel.handoff-panel-docked {
  width: 50vw;
  height: 100vh;
  max-height: 100vh;
  min-width: 0;
  border-radius: 0;
  pointer-events: auto;
}
.handoff-resize-handle {
  position: absolute;
  z-index: 1;
  top: 0;
  bottom: 0;
  left: 15vw;
  width: 8px;
  cursor: col-resize;
  touch-action: none;
  pointer-events: auto;
}
.handoff-resize-handle::after {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 2px;
  background: var(--border);
  content: '';
}
.handoff-resize-handle:hover::after,
.handoff-resize-handle:focus-visible::after {
  background: var(--accent);
}
.handoff-panel header, .handoff-panel footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}
.handoff-panel p { margin: 4px 0 0; color: var(--text-dim); }
.handoff-status { color: var(--accent); font-weight: 600; }
.handoff-screen {
  position: relative;
  display: grid;
  min-height: 250px;
  place-items: center;
  overflow: auto;
  background: #111;
}
.handoff-screen img {
  display: block;
  width: 100%;
  height: auto;
  cursor: crosshair;
  touch-action: none;
  user-select: none;
}
.handoff-loading { position: absolute; color: white; pointer-events: none; }
.handoff-panel footer span { color: var(--text-dim); font-size: 13px; }
.handoff-panel footer button {
  flex: 0 0 auto;
  padding: 9px 14px;
  border: 0;
  border-radius: var(--radius-sm);
  background: var(--accent);
  color: white;
  cursor: pointer;
}
.handoff-panel button:disabled { opacity: .6; cursor: wait; }
.handoff-error { color: var(--err) !important; }
@media (max-width: 640px) {
  .handoff-panel { padding: 10px; }
  .handoff-panel.handoff-panel-docked { width: 100vw; }
  .handoff-panel header, .handoff-panel footer { align-items: flex-start; flex-direction: column; }
  .handoff-panel header button, .handoff-panel footer button { width: 100%; }
}
</style>
