<script setup lang="ts">
import { computed } from "vue";

import type { ToastMessage } from "../types/contracts";

const props = defineProps<{
  toast: ToastMessage;
}>();

const emit = defineEmits<{
  close: [id: string];
}>();

const role = computed(() => (props.toast.kind === "error" ? "alert" : "status"));
const liveMode = computed(() => (props.toast.kind === "error" ? "assertive" : "polite"));
const kindLabel = computed(() => {
  const labels = { info: "提示", success: "成功", error: "错误" } as const;
  return labels[props.toast.kind];
});
</script>

<template>
  <article
    class="toast"
    :class="`is-${toast.kind}`"
    :role="role"
    :aria-live="liveMode"
  >
    <span class="toast-dot" aria-hidden="true" />
    <div class="toast-copy">
      <span class="sr-only">{{ kindLabel }}：</span>
      <strong>{{ toast.title }}</strong>
      <p>{{ toast.message }}</p>
    </div>
    <button type="button" class="toast-close" aria-label="关闭提示" @click="emit('close', toast.id)">
      ×
    </button>
  </article>
</template>

<style scoped>
.toast {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: start;
  gap: 9px;
  padding: 11px 10px 11px 12px;
  border: 1px solid var(--border, #e1e6ef);
  border-radius: 12px;
  color: var(--text, #171e2e);
  background: var(--surface, #fff);
  box-shadow: var(--shadow-float, 0 18px 55px rgb(15 23 42 / 16%));
  animation: toast-in 160ms ease-out;
  pointer-events: auto;
}

.toast-dot {
  width: 7px;
  height: 7px;
  margin-top: 5px;
  border-radius: 999px;
  background: var(--brand, #6258e7);
}

.toast.is-error .toast-dot {
  background: var(--danger, #a93643);
}

.toast.is-success .toast-dot {
  background: var(--success, #0e735f);
}

.toast-copy {
  min-width: 0;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.toast strong,
.toast p {
  display: block;
  margin: 0;
}

.toast strong {
  font-size: 13px;
}

.toast p {
  margin-top: 2px;
  color: var(--muted, #596478);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.toast-close {
  display: grid;
  width: 26px;
  height: 26px;
  place-items: center;
  margin: -4px -3px 0 0;
  padding: 0;
  border: 0;
  border-radius: 8px;
  color: var(--muted, #596478);
  font: inherit;
  font-size: 18px;
  line-height: 1;
  background: transparent;
  cursor: pointer;
}

.toast-close:hover {
  color: var(--text, #171e2e);
  background: var(--surface-strong, #eef1f6);
}

@keyframes toast-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
}
</style>
