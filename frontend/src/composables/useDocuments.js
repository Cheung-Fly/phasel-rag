/* ============================================================
   useDocuments —— 知识库文档的列表 / 上传 / 删除
   ============================================================ */
import { reactive, ref } from 'vue';
import { api } from '../lib/api.js';
import { useToast } from './useToast.js';
import { useHealth } from './useHealth.js';
import { useTasks } from './useTasks.js';

const documents = ref([]);
const listError = ref('');
const importingUrl = ref(false);

/**
 * 上传进度面板的状态。
 * active 一旦置 true 就不再收起 —— 保留「入库完成：N 个片段」这个结果，
 * 让用户能确认刚才那次上传到底成功了没有。如果传完就消失，
 * 用户很容易怀疑「是不是没传上去」而重复上传。
 */
const uploader = reactive({
  active: false,
  name: '',
  pct: 0,
  status: '',
  ok: null,        // true 成功 / false 失败 / null 进行中
});

export function useDocuments() {
  const { toast } = useToast();
  const { maxUploadMb } = useHealth();
  const { refresh: refreshTasks } = useTasks();

  async function refresh() {
    try {
      const d = await api.listDocuments();
      documents.value = d.documents || [];
      listError.value = '';
    } catch (e) {
      documents.value = [];
      listError.value = e.message;
    }
  }

  async function remove(docId) {
    if (!confirm('确定删除这份文档？对应的向量数据会一起删除，不可恢复。')) return;
    try {
      await api.deleteDocument(docId);
      toast('已删除');
      await refresh();
    } catch (e) {
      toast('删除失败：' + e.message, 'error');
    }
  }

  /**
   * 上传文档。
   *
   * 注意这里的校验只是「省掉一次注定失败的上传」，
   * 真正的权威校验在后端（而且后端还有字符数 / 切片数两道闸门）。
   * 前端不做任何「我以为后端会拒绝所以先拦下来」的判断，
   * 否则前后端规则一不一致就会拦掉合法文件。
   *
   * ★ 2026-10-14 阶段 B：上传的语义变了。响应只代表「已受理」，
   *   入库在后台任务里进行。所以这里【不能】再显示「入库完成」——
   *   那会是一个骗人的结论。改为：收起上传进度条（它的 100% 只表示
   *   字节传完），把进度交给「后台任务」面板，并立刻刷一次任务列表，
   *   否则用户要等最多 15 秒（空闲轮询间隔）才看见任务出现。
   *   降级路径（mode=sync，队列不可用）仍是同步语义，保持原样。
   */
  async function upload(file) {
    if (!file) return;

    const limit = maxUploadMb.value;
    if (file.size > limit * 1024 * 1024) {
      toast(`文件超过 ${limit}MB 限制`, 'error');
      return;
    }

    uploader.active = true;
    uploader.name = file.name;
    uploader.pct = 0;
    uploader.status = '上传中…';
    uploader.ok = null;

    try {
      const info = await api.uploadDocument(file, {
        onProgress: (pct) => {
          uploader.pct = pct;
          uploader.status = `上传中… ${pct.toFixed(0)}%`;
        },
      });

      if (info.mode === 'queued') {
        uploader.active = false;
        uploader.status = '';
        uploader.ok = null;
        toast(`已提交「${info.filename || file.name}」，正在后台入库`);
        await refreshTasks();
        return;
      }

      uploader.pct = 100;
      const n = info.chunks ?? '?';
      uploader.status = `入库完成：${n} 个片段`;
      uploader.ok = true;
      toast(`已入库 ${info.filename || file.name}（${n} 片段）`);
      await refresh();
    } catch (e) {
      uploader.status = '失败：' + e.message;
      uploader.ok = false;
      toast('入库失败：' + e.message, 'error');
    }
  }

  async function importWebPage(url) {
    if (!url || importingUrl.value) return false;
    importingUrl.value = true;
    try {
      const info = await api.importWebPage(url);
      if (info.mode === 'queued') {
        toast(`已提交「${info.filename || '网页'}」，正在后台入库`);
        await refreshTasks();
      } else {
        toast(`已将网页入库（${info.chunks ?? '?'} 个片段）`);
        await refresh();
      }
      return true;
    } catch (e) {
      toast(`网页导入失败：${e.message}`, 'error');
      return false;
    } finally {
      importingUrl.value = false;
    }
  }

  return { documents, listError, uploader, importingUrl, refresh, remove, upload, importWebPage };
}
