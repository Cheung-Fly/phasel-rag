<script setup>
/* ============================================================
   HealthStatus —— 后端状态（左下角）
   ------------------------------------------------------------
   四种状态，映射到三种颜色：

     ok        绿色  模型名 + 记忆后端标签
     degraded  黄色  服务在，但向量库不可达 —— 能连上却答不了题
     error     红色  请求本身失败，服务可能没起来
     unknown   灰色  首次探测还没回来

   ★ 为什么要把 degraded 单独拎出来：
     /health 一直有 chroma_status 字段，但迁移前的前端只判断
     status === 'ok'，其余一律显示红色「异常」。结果是「向量库挂了」
     和「整台机器不通」看起来一模一样 —— 前者刷新没用、得去修服务，
     后者多半是自己网络断了。这一字之差决定了用户下一步做什么。
   ============================================================ */
import { computed } from 'vue';
import { useHealth } from '../composables/useHealth.js';

const { status, llmModel, memoryBackend } = useHealth();

const memTitle = '记忆/存储后端：会话永久存档在 PostgreSQL，Redis 只作热缓存';

const dotClass = computed(() => {
  if (status.value === 'ok') return 'dot-ok';
  if (status.value === 'degraded') return 'dot-warn';
  if (status.value === 'error') return 'dot-err';
  return 'dot-gray';
});

const label = computed(() => {
  if (status.value === 'ok') return llmModel.value;
  if (status.value === 'degraded') return '向量库不可用';
  if (status.value === 'error') return '无法连接';
  return '检测中…';
});
</script>

<template>
  <section class="panel">
    <h3 class="panel-title">后端状态</h3>
    <div class="health">
      <span class="dot" :class="dotClass"></span>
      <span>{{ label }}</span>
      <span v-if="status === 'ok' && memoryBackend" class="mem-tag" :title="memTitle">
        {{ memoryBackend }}
      </span>
    </div>
  </section>
</template>
