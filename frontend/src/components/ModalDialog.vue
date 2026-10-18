<script setup>
import { onMounted, onUnmounted } from 'vue';

defineProps({ title: { type: String, required: true } });
const emit = defineEmits(['close']);

function onKey(e) {
  if (e.key === 'Escape') emit('close');
}
onMounted(() => window.addEventListener('keydown', onKey));
onUnmounted(() => window.removeEventListener('keydown', onKey));
</script>

<template>
  <Teleport to="body">
    <div class="modal-backdrop" @click.self="emit('close')">
      <section class="modal-window" role="dialog" aria-modal="true" :aria-label="title">
        <header>
          <h3>{{ title }}</h3>
          <button type="button" class="btn-ghost" @click="emit('close')">关闭</button>
        </header>
        <div class="modal-body"><slot /></div>
      </section>
    </div>
  </Teleport>
</template>

<style>
.modal-backdrop {
  position: fixed; inset: 0; z-index: 50;
  display: grid; place-items: center; padding: 18px;
  background: rgb(0 0 0 / 60%); overflow: auto;
}
.modal-window {
  display: grid; gap: 10px; grid-template-rows: auto 1fr;
  width: min(560px, 100%); max-height: 86vh; padding: 18px;
  border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--bg-panel); box-shadow: var(--shadow);
}
.modal-window header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.modal-window h3 { margin: 0; font-size: 15px; }
.modal-body { display: grid; gap: 10px; overflow: auto; min-height: 0; align-content: start; }
.modal-body h4 { margin: 4px 0 0; font-size: 12px; color: var(--text-dim); }
.modal-list { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; font-size: 12px; color: var(--text-dim); }
.modal-list li { display: flex; align-items: center; gap: 8px; }
.modal-list li > span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.modal-form { display: grid; gap: 6px; }
.modal-form input, .modal-form textarea {
  min-width: 0; border: 1px solid var(--border); border-radius: 6px; padding: 7px;
  background: var(--surface); color: var(--text); font: inherit; font-size: 12px;
}
.modal-form textarea { min-height: 160px; resize: vertical; font-family: ui-monospace, Consolas, monospace; }
.modal-msg { margin: 0; font-size: 11.5px; color: var(--text-faint); }
.modal-msg.err { color: var(--err); }
.link-button { border: 0; background: transparent; color: var(--accent); cursor: pointer; font-size: 12px; padding: 0 3px; }
</style>
