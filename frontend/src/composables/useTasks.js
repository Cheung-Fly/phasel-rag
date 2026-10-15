/* ============================================================
   useTasks —— 后台任务的列表 / 轮询 / 取消
   ------------------------------------------------------------
   ★ 为什么轮询间隔是「自适应」而不是固定 2 秒
     这台机器只有 2 vCPU。任务跑完之后（也就是绝大多数时间）还每 2 秒
     打一次接口，是纯粹的空转。所以：有活动任务时 2 秒（进度要跟得上），
     没有任务时退到 15 秒（只为发现别处提交的任务）。代价与收益才对得上。

   ★ 为什么上传之后要立刻 refresh 一次（见 useDocuments）
     空闲期的轮询是 15 秒。如果只顾轮询，用户点完上传要盯着「已提交」
     十几秒才看见任务出现在面板里，会以为没提交成功。

   ★ 为什么这里只管「任务是什么状态」，不管「入库后文档列表要刷新」
     那是两个数据源的联动，放在组件里显式写出来更容易看懂，
     也避免 composable 之间互相依赖。
   ============================================================ */
import { computed, ref } from 'vue';
import { api } from '../lib/api.js';
import { useToast } from './useToast.js';

/** 未终结的状态。UI 用它决定「转圈还是给结论」。 */
export const ACTIVE_STATUSES = new Set(['queued', 'running']);

const POLL_ACTIVE_MS = 2000;
const POLL_IDLE_MS = 15000;

const tasks = ref([]);
const listError = ref('');

let timer = null;

const hasActive = computed(() => tasks.value.some((task) => ACTIVE_STATUSES.has(task.status)));
const activeCount = computed(() => tasks.value.filter((task) => ACTIVE_STATUSES.has(task.status)).length);

function isActive(task) {
  return ACTIVE_STATUSES.has(task?.status);
}

async function refresh() {
  try {
    const data = await api.listTasks();
    tasks.value = data.tasks || [];
    listError.value = '';
  } catch (e) {
    // 保留上一次的列表而不是清空：一次接口抖动就把面板清空，
    // 用户会以为任务丢了。只有错误提示更新。
    listError.value = e.message;
  }
}

function schedule() {
  if (timer) clearTimeout(timer);
  timer = setTimeout(async () => {
    await refresh();
    schedule();
  }, hasActive.value ? POLL_ACTIVE_MS : POLL_IDLE_MS);
}

export function useTasks() {
  const { toast } = useToast();

  function start() {
    stop();
    refresh().then(schedule);
  }

  function stop() {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  /**
   * 请求取消。
   * 注意后端有两种结局，这里不做乐观更新（不直接把状态改成「已取消」）：
   * 运行中的任务只能协作式停止，真实状态要等 worker 在检查点停下才变。
   * 用「立刻刷新」把判断权留给后端，前端只负责把结果如实显示出来。
   */
  async function cancel(taskId) {
    try {
      await api.cancelTask(taskId);
      await refresh();
    } catch (e) {
      toast('取消失败：' + e.message, 'error');
    }
  }

  return { tasks, listError, hasActive, activeCount, isActive, refresh, start, stop, cancel };
}
