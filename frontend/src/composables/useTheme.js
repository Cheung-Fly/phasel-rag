/* ============================================================
   useTheme —— 主题（深色 / 浅色 / 跟随系统）
   ------------------------------------------------------------
   三态设计：
     null     跟随系统
     'light'  强制浅色
     'dark'   强制深色
   存的是「用户的选择」而不是「当前颜色」。如果只存最终颜色，
   「跟随系统」这个选项就没法表达了。

   实现要点：
     主题不是靠切换 CSS 类，而是改 <html> 上的 data-theme 属性，
     style.css 里用 :root[data-theme="light"] 覆盖一组颜色变量。
     好处是布局样式完全不用关心主题。

   ★ 首帧闪烁：本文件负责的是「运行期切换」和「挂载时对齐」。
     真正防止深色用户先看到一帧白屏的，是 index.html 里那段内联脚本，
     它在 HTML 解析阶段就把 data-theme 打上去了。
   ============================================================ */
import { computed, ref } from 'vue';
import { getTheme, setTheme } from '../lib/storage.js';
import { useToast } from './useToast.js';

const theme = ref(getTheme());   // 'light' | 'dark' | null

function systemPrefersLight() {
  // matchMedia 在极老浏览器上不存在，包一层避免整个应用挂掉
  return typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: light)').matches
    : false;
}

/** 由「选择」推导出「此刻是不是浅色」 */
function isLightOf(t) {
  return t === 'light' || (!t && systemPrefersLight());
}

const isLight = ref(isLightOf(theme.value));

/** 把当前选择落到 DOM 上。任何改变 theme 的地方都要调它。 */
function apply() {
  const t = theme.value;
  if (t) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  isLight.value = isLightOf(t);
}

export function useTheme() {
  const { toast } = useToast();

  /**
   * 在两种「明确颜色」之间切换：当前浅 → 深，当前深 → 浅
   * 注意：用户处在「跟随系统」时，第一次点击会偏向系统当前颜色的反面，
   * 之后就是明确的浅↔深。这是刻意的 —— 否则在浅色系统上一键下去
   * 看不出任何变化，用户会以为按钮坏了。
   */
  function toggle() {
    const next = isLight.value ? 'dark' : 'light';
    theme.value = next;
    setTheme(next);
    apply();
    // 提示放在这里而不是调用方：无论从按钮点还是 Ctrl+/ 触发的，
    // 都该有一致的反馈，且只可能有一处漏写。
    toast(next === 'light' ? '已切换为浅色' : '已切换为深色');
    return next;
  }

  // 按钮上的图标：显示「点击后会变成什么」
  const icon  = computed(() => (isLight.value ? '☾' : '☀'));
  const title = computed(() => (isLight.value ? '切换到深色' : '切换到浅色'));

  return { theme, isLight, icon, title, apply, toggle };
}
