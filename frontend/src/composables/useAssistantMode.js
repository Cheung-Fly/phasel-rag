import { ref } from 'vue';

function storedMode() {
  try {
    return localStorage.getItem('rag.assistantMode') === 'student' ? 'student' : 'school';
  } catch {
    return 'school';
  }
}

const mode = ref(storedMode());

export function useAssistantMode() {
  return mode;
}

export function setAssistantMode(value) {
  if (!['school', 'student'].includes(value)) return;
  mode.value = value;
  try {
    localStorage.setItem('rag.assistantMode', value);
  } catch {
    // The current tab still switches modes when browser storage is unavailable.
  }
}
