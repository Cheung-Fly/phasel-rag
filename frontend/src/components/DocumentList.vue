<script setup>
/* ============================================================
   DocumentList —— 已入库文档列表
   ============================================================ */
import { useDocuments } from '../composables/useDocuments.js';

const { documents, listError, remove } = useDocuments();
</script>

<template>
  <section class="panel">
    <h3 class="panel-title">
      已入库文档 <span class="count">{{ documents.length }}</span>
    </h3>

    <ul class="doc-list">
      <!-- 三种状态互斥，顺序有意义：先报错、再空、最后才是列表 -->
      <li v-if="listError" class="empty">读取失败：{{ listError }}</li>
      <li v-else-if="!documents.length" class="empty">还没有文档</li>
      <template v-else>
        <li v-for="doc in documents" :key="doc.doc_id">
          <span class="doc-name" :title="doc.filename">{{ doc.filename }}</span>
          <button class="doc-del" title="删除" @click="remove(doc.doc_id)">×</button>
        </li>
      </template>
    </ul>
  </section>
</template>
