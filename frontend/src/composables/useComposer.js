/* ============================================================
   useComposer —— 输入框的「聚焦」信号
   ------------------------------------------------------------
   一个纯粹的解耦小工具，解决的是这个具体问题：

     需要让输入框获得焦点的地方有三处 ——
       1) 全局快捷键 Ctrl+K（在 App.vue 里监听）
       2) 每次问答结束后（在 useChat 里）
       3) 应用启动完成时（在 App.vue 的初始化里）
     而真正持有 <textarea> 的是 ChatComposer 组件。

     最直接的写法是到处 document.getElementById('question').focus()，
     但那等于绕过组件边界去摸 DOM，一旦以后有多个输入框就会失控。

   做法：这里只维护一个自增计数器。需要聚焦的一方调 requestFocus()，
   ChatComposer 监听这个计数器变化后自己去 focus。
   数据层完全不认识 DOM，组件也无需被外部拿到 ref。
   ============================================================ */
import { ref } from 'vue';

const focusTick = ref(0);

export function useComposer() {
  function requestFocus() {
    focusTick.value++;
  }
  return { focusTick, requestFocus };
}
