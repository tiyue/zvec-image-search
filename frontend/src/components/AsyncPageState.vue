<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  label?: string;
  error?: unknown;
}>();

const emit = defineEmits<{
  retry: [];
}>();

const labelText = computed(() => props.label ?? "页面");

/**
 * Doubles as the async-component loading and error surface: an absent error
 * renders the spinner, a present error renders a short user-facing notice plus
 * a manual retry. The raw error stays in Vue's async plumbing for diagnostics
 * and is never shown as product copy. Nothing here re-requests on its own.
 */
const hasError = computed(() => {
  const value = props.error;
  return !(value === null || value === undefined || value === "");
});
</script>

<template>
  <div class="async-page-state" :class="{ 'is-error': hasError }" :aria-busy="!hasError">
    <template v-if="hasError">
      <strong>无法加载{{ labelText }}</strong>
      <p>请重试加载此页面。</p>
      <button class="async-page-retry" type="button" data-testid="async-page-retry" @click="emit('retry')">
        重试
      </button>
    </template>
    <template v-else>
      <span class="async-page-spinner" aria-hidden="true" />
      <span>正在加载{{ labelText }}…</span>
    </template>
  </div>
</template>

<style scoped>
.async-page-state {
  display: flex;
  min-height: 240px;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 10px;
  padding: 24px;
  color: var(--muted, #6f6f6f);
  font-size: 13px;
  text-align: center;
}

.async-page-state strong {
  color: var(--text, #0d0d0d);
  font-size: 15px;
}

.async-page-state p {
  max-width: 440px;
  margin: 0;
  overflow-wrap: anywhere;
}

.async-page-spinner {
  width: 24px;
  height: 24px;
  border: 3px solid rgb(0 0 0 / 12%);
  border-top-color: currentcolor;
  border-radius: 999px;
  animation: async-page-spin 700ms linear infinite;
}

.async-page-retry {
  min-height: 34px;
  padding: 0 16px;
  border: 1px solid var(--border-strong, rgb(0 0 0 / 14%));
  border-radius: 9px;
  color: var(--text, #0d0d0d);
  font: inherit;
  font-size: 13px;
  font-weight: 700;
  background: var(--surface-soft, #f7f7f7);
  cursor: pointer;
}

@keyframes async-page-spin {
  to {
    transform: rotate(1turn);
  }
}

@media (prefers-color-scheme: dark) {
  .async-page-state {
    --text: #ededed;
    --muted: #b5b5b5;
    --border-strong: rgb(255 255 255 / 22%);
    --surface-soft: #2b2b2b;
    color-scheme: dark;
    color: #b5b5b5;
  }

  .async-page-spinner {
    border-color: rgb(255 255 255 / 16%);
  }
}

@media (prefers-reduced-motion: reduce) {
  .async-page-spinner {
    animation: none;
  }
}
</style>
