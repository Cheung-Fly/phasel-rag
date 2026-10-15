import { ref } from 'vue';

const active = ref(false);
const available = ref(false);

export function useBrowserHandoff() {
  return { active, available };
}

export function setBrowserHandoffActive(value) {
  active.value = value;
  if (value) available.value = true;
}

export function setBrowserHandoffAvailable(value) {
  available.value = value;
  if (!value) active.value = false;
}
