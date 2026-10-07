/* ============================================================
   useToast —— 轻提示
   ------------------------------------------------------------
   为什么写成「模块级单例」而不是每次调用新建一个 ref：
     提示条在页面上只有一条，且任何组件（上传失败、复制成功、
     会话删除）都可能触发它。如果每个组件各持一份状态，
     就会出现「同一条提示在不同位置冒出来」的怪现象。
     把状态放在模块作用域，所有调用方共享同一个实例 ——
     这是 Vue 组合式 API 里最轻量的跨组件共享方案（无需 Pinia）。
   ============================================================ */
import { ref } from 'vue';

const message = ref('');
const kind = ref('info');     // 'info' | 'error'
const visible = ref(false);

let timer = null;

export function useToast() {
  /**
   * @param {string} msg  提示内容
   * @param {'info'|'error'} k  样式种类
   */
  function toast(msg, k = 'info') {
    message.value = String(msg);
    kind.value = k;
    visible.value = true;
    // 连续触发时重置计时，而不是叠加多个定时器 ——
    // 否则上一条的定时器会把新提示提前关掉。
    clearTimeout(timer);
    timer = setTimeout(() => { visible.value = false; }, 2600);
  }

  return { message, kind, visible, toast };
}
