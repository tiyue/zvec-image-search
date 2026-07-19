<script setup lang="ts">
import { computed, ref, useId, watch } from "vue";

const props = withDefaults(defineProps<{
  page: number;
  totalPages: number;
  totalItems: number;
  loading: boolean;
  hasPrevious: boolean;
  hasNext: boolean;
  ariaLabel?: string;
  itemLabel?: string;
  idPrefix?: string;
}>(), {
  ariaLabel: "搜索结果分页",
  itemLabel: "张",
  idPrefix: "",
});

const emit = defineEmits<{
  previous: [];
  next: [];
  jump: [page: number];
}>();

const pageInput = ref<HTMLInputElement | null>(null);
const draftPage = ref(String(Math.max(1, Math.trunc(props.page || 1))));
const validationMessage = ref("");
const generatedId = useId().replace(/[^a-zA-Z0-9_-]/gu, "-");
const controlPrefix = computed(() => props.idPrefix.trim() || `pagination-${generatedId}`);
const inputId = computed(() => `${controlPrefix.value}-page-input`);
const errorId = computed(() => `${controlPrefix.value}-page-error`);

const hasResults = computed(() => props.totalItems > 0 && props.totalPages > 0);
const safePage = computed(() => Math.max(1, Math.trunc(props.page || 1)));
const safeTotalPages = computed(() => Math.max(0, Math.trunc(props.totalPages || 0)));
const previousDisabled = computed(
  () => props.loading || !props.hasPrevious || !hasResults.value || safePage.value <= 1,
);
const nextDisabled = computed(
  () =>
    props.loading ||
    !props.hasNext ||
    !hasResults.value ||
    safePage.value >= safeTotalPages.value,
);

watch(
  () => props.page,
  (value) => {
    draftPage.value = String(Math.max(1, Math.trunc(value || 1)));
    validationMessage.value = "";
  },
);

function clearValidation(): void {
  validationMessage.value = "";
}

function submitJump(): void {
  // Number inputs may supply a numeric value through Vue/test adapters even
  // though the ref starts as text. Normalizing here keeps validation stable.
  const raw = String(draftPage.value ?? "").trim();
  if (!/^\d+$/u.test(raw)) {
    validationMessage.value = "请输入有效页码";
    return;
  }
  const target = Number(raw);
  if (!Number.isSafeInteger(target) || target < 1 || target > safeTotalPages.value) {
    validationMessage.value = `请输入 1～${Math.max(1, safeTotalPages.value)} 之间的页码`;
    return;
  }
  validationMessage.value = "";
  if (target !== safePage.value) emit("jump", target);
}

function focusInput(): void {
  pageInput.value?.focus({ preventScroll: true });
  pageInput.value?.select();
}

defineExpose({ focusInput });
</script>

<template>
  <nav class="gallery-pagination" :aria-label="ariaLabel">
    <button
      class="button button-quiet button-small pagination-edge"
      type="button"
      :disabled="previousDisabled"
      aria-label="首页"
      @click="emit('jump', 1)"
    >
      首页
    </button>
    <button
      class="button button-quiet button-small"
      type="button"
      :disabled="previousDisabled"
      aria-label="上一页"
      @click="emit('previous')"
    >
      上一页
    </button>
    <form class="pagination-jump" @submit.prevent="submitJump">
      <label :for="inputId">第</label>
      <input
        :id="inputId"
        ref="pageInput"
        v-model="draftPage"
        type="number"
        inputmode="numeric"
        min="1"
        :max="Math.max(1, safeTotalPages)"
        step="1"
        :disabled="loading || !hasResults"
        :aria-invalid="Boolean(validationMessage)"
        :aria-describedby="validationMessage ? errorId : undefined"
        aria-label="跳转页码"
        @input="clearValidation"
        @keydown.enter.prevent="submitJump"
      />
      <span>/ {{ safeTotalPages }} 页</span>
      <button class="button button-secondary button-small" type="submit" :disabled="loading || !hasResults">
        跳转
      </button>
      <small :id="errorId" class="pagination-error" role="alert">{{ validationMessage }}</small>
    </form>
    <span class="pagination-copy" aria-live="polite">
      <strong>{{ hasResults ? `当前第 ${safePage} 页` : "暂无结果" }}</strong>
      <small>共 {{ Math.max(0, totalItems) }} {{ itemLabel }}</small>
    </span>
    <button
      class="button button-quiet button-small"
      type="button"
      :disabled="nextDisabled"
      aria-label="下一页"
      @click="emit('next')"
    >
      下一页
    </button>
    <button
      class="button button-quiet button-small pagination-edge"
      type="button"
      :disabled="nextDisabled"
      aria-label="末页"
      @click="emit('jump', safeTotalPages)"
    >
      末页
    </button>
  </nav>
</template>

<style scoped>
.gallery-pagination {
  display: flex;
  min-height: 52px;
  align-items: center;
  justify-content: center;
  gap: 9px;
  padding-top: 10px;
  font-size: 13px;
}

.button {
  min-height: 38px;
  padding: 8px 12px;
  border: 1px solid var(--border, #e1e6ef);
  border-radius: 10px;
  color: var(--text, #171e2e);
  font: inherit;
  font-size: 13px;
  font-weight: 700;
  background: var(--surface, #fff);
  cursor: pointer;
}

.button:not(:disabled):hover {
  border-color: var(--border-strong, #cbd3df);
  background: var(--surface-strong, #eef1f6);
}

.button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.gallery-pagination .button {
  min-width: 72px;
}

.pagination-edge {
  min-width: 56px !important;
}

.pagination-jump {
  position: relative;
  display: flex;
  min-height: 42px;
  align-items: center;
  gap: 7px;
  padding: 3px 7px;
  border: 1px solid var(--border, #e1e6ef);
  border-radius: 11px;
  color: var(--muted, #596478);
  background: var(--surface-soft, #f8f9fc);
  white-space: nowrap;
}

.pagination-jump label,
.pagination-jump span {
  font-weight: 600;
}

.pagination-jump input {
  width: 64px;
  height: 34px;
  padding: 0 7px;
  border: 1px solid var(--border-strong, #cbd3df);
  border-radius: 8px;
  color: var(--text, #171e2e);
  font: inherit;
  font-variant-numeric: tabular-nums;
  text-align: center;
  background: var(--surface, #fff);
}

.pagination-jump input:focus {
  outline: 0;
  border-color: var(--brand, #6258e7);
  box-shadow: 0 0 0 3px rgb(98 88 231 / 14%);
}

.pagination-jump input[aria-invalid="true"] {
  border-color: var(--danger, #a93643);
}

.pagination-error {
  position: absolute;
  top: calc(100% + 3px);
  left: 10px;
  z-index: 3;
  min-width: max-content;
  color: var(--danger, #a93643);
  font-size: 12px;
  font-weight: 600;
}

.pagination-copy {
  display: grid;
  flex: 0 0 104px;
  gap: 1px;
  color: var(--muted, #596478);
  text-align: center;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.pagination-copy strong {
  color: var(--text, #171e2e);
  font-size: 13px;
}

.pagination-copy small {
  font-size: 12px;
}

@media (max-width: 1100px) {
  .gallery-pagination {
    flex-wrap: wrap;
    gap: 7px;
  }

  .pagination-copy {
    flex-basis: 96px;
  }
}

@media (max-width: 760px) {
  .pagination-edge {
    display: none;
  }

  .pagination-jump {
    order: 3;
    width: 100%;
    justify-content: center;
  }
}
</style>
