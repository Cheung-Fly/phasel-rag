/* ============================================================
   useDocuments —— 知识库文档的列表 / 上传 / 删除
   ============================================================ */
import { reactive, ref } from 'vue';
import { api } from '../lib/api.js';
import { useToast } from './useToast.js';
import { useHealth } from './useHealth.js';

const documents = ref([]);
const listError = ref('');

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
   * 上传并入库。
   *
   * 注意这里的校验只是「省掉一次注定失败的上传」，
   * 真正的权威校验在后端（而且后端还有字符数 / 切片数两道闸门）。
   * 前端不做任何「我以为后端会拒绝所以先拦下来」的判断，
   * 否则前后端规则一不一致就会拦掉合法文件。
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

  return { documents, listError, uploader, refresh, remove, upload };
}
