/* ============================================================
   useHealth —— 后端健康状态 + 由后端下发的运行参数
   ------------------------------------------------------------
   两个职责放在一起，是因为它们来自同一个 /health 响应，
   分两个请求去取纯属浪费（而这台机器只有 2 vCPU，能省则省）。

   ★ 关键设计：上传上限由后端说了算
     前端绝不写死「最大 20MB / 50MB」这类数字。上限是配置项，
     会随机型与 .env 调整；写死就会出现「界面说 20MB、后端其实收 50MB」
     这种一眼假的信息。这里读 /health 的 max_upload_mb 并显示，
     顺带做一次本地预校验（省掉注定失败的 50MB 上传）。

   ★ 为什么「先取参数、再判状态」
     上传上限是配置，跟向量库健康与否无关。后端降级（chroma 不可达）时
     它依然有效，所以不能把 applyLimits 塞进 status === 'ok' 分支里 ——
     否则一次向量库抖动会让界面上限退回占位值，显示成错误的数字。
   ============================================================ */
import { ref } from 'vue';
import { api } from '../lib/api.js';

const status         = ref('unknown');   // 'ok' | 'degraded' | 'error' | 'unknown'
const llmModel       = ref('');
const memoryBackend  = ref('');
const chromaStatus   = ref('');
const maxUploadMb    = ref(50);          // 占位初值，真实值由 /health 覆盖
const ingestMaxChunks = ref(null);

const INTERVAL_MS = 30000;
let timer = null;

async function check() {
  try {
    const d = await api.health();

    // —— 配置项优先，与健康状态无关 ——
    const mb = Number(d && d.max_upload_mb);
    if (mb) maxUploadMb.value = mb;
    if (d && d.ingest_max_chunks) ingestMaxChunks.value = d.ingest_max_chunks;

    llmModel.value      = d.llm_model      || '';
    memoryBackend.value = d.memory_backend || '';
    chromaStatus.value  = d.chroma_status  || '';

    // 后端在向量库不可达时会把 status 降级为 degraded，
    // 这时 API 进程本身是活的，但知识库功能已经不可用 —— 必须区分开，
    // 否则用户看到「后端正常」却在提问时一直失败，会以为是自己的问题。
    status.value = d.status === 'ok' ? 'ok' : 'degraded';
  } catch {
    status.value = 'error';
  }
}

export function useHealth() {
  /** 立即探一次，并开始周期轮询 */
  function start() {
    check();
    if (timer) clearInterval(timer);
    timer = setInterval(check, INTERVAL_MS);
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  return {
    status, llmModel, memoryBackend, chromaStatus,
    maxUploadMb, ingestMaxChunks,
    check, start, stop,
  };
}
