<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(defineProps<{
  visible: boolean;
  x: number;
  y: number;
  selectionCount: number;
  exporting: boolean;
  mode?: "search" | "recommendation";
  actionPending?: boolean;
  feedbackAvailable?: boolean;
  feedbackPending?: boolean;
  feedbackAction?: string;
}>(), {
  mode: "search",
  actionPending: false,
  feedbackAvailable: false,
  feedbackPending: false,
  feedbackAction: "",
});

const recommendationMode = computed(() => props.mode === "recommendation");

const emit = defineEmits<{
  detail: [];
  open: [];
  reveal: [];
  copyImage: [];
  copyFiles: [];
  copyPaths: [];
  export: [];
  markRelevant: [];
  markNotRelevant: [];
  like: [];
  dislike: [];
  undoFeedback: [];
  clear: [];
  close: [];
}>();
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="gallery-context-menu"
      role="menu"
      :aria-label="recommendationMode ? '推荐图片操作' : '图片操作'"
      :style="{ left: `${x}px`, top: `${y}px` }"
      @click.stop
      @contextmenu.prevent
    >
      <header v-if="!recommendationMode">
        <strong>已选择 {{ selectionCount }} 张</strong>
        <small>支持 Ctrl / Shift 跨页保留选择</small>
      </header>
      <button type="button" role="menuitem" @click="emit('detail'); emit('close')">打开详情</button>
      <button
        type="button"
        role="menuitem"
        :disabled="recommendationMode && actionPending"
        @click="emit('open'); emit('close')"
      >系统打开</button>
      <button
        type="button"
        role="menuitem"
        :disabled="recommendationMode && actionPending"
        @click="emit('reveal'); emit('close')"
      >所在文件夹</button>
      <hr />
      <template v-if="feedbackAvailable">
        <template v-if="recommendationMode">
          <button
            class="feedback-positive"
            type="button"
            role="menuitemradio"
            :aria-checked="feedbackAction === 'like'"
            :disabled="actionPending || feedbackPending"
            @click="emit('like'); emit('close')"
          >
            {{ feedbackAction === "like" ? "✓ 已喜欢" : "喜欢" }}
          </button>
          <button
            class="feedback-negative"
            type="button"
            role="menuitemradio"
            :aria-checked="feedbackAction === 'dislike'"
            :disabled="actionPending || feedbackPending"
            @click="emit('dislike'); emit('close')"
          >
            {{ feedbackAction === "dislike" ? "✓ 已不喜欢" : "不喜欢" }}
          </button>
        </template>
        <template v-else>
          <button
            class="feedback-positive"
            type="button"
            role="menuitemradio"
            :aria-checked="feedbackAction === 'relevant'"
            :disabled="selectionCount !== 1 || feedbackPending"
            @click="emit('markRelevant'); emit('close')"
          >
            {{ feedbackAction === "relevant" ? "✓ 已标记为相关" : "标记为相关" }}
          </button>
          <button
            class="feedback-negative"
            type="button"
            role="menuitemradio"
            :aria-checked="feedbackAction === 'not_relevant'"
            :disabled="selectionCount !== 1 || feedbackPending"
            @click="emit('markNotRelevant'); emit('close')"
          >
            {{ feedbackAction === "not_relevant" ? "✓ 已标记为不相关" : "标记为不相关" }}
          </button>
          <button
            v-if="feedbackAction"
            class="is-muted"
            type="button"
            role="menuitem"
            :disabled="feedbackPending"
            @click="emit('undoFeedback'); emit('close')"
          >
            撤销反馈
          </button>
        </template>
        <hr />
      </template>
      <button
        type="button"
        role="menuitem"
        :disabled="selectionCount !== 1 || (recommendationMode && actionPending)"
        @click="emit('copyImage'); emit('close')"
      >
        复制图片
      </button>
      <button
        type="button"
        role="menuitem"
        :disabled="recommendationMode && actionPending"
        @click="emit('copyFiles'); emit('close')"
      >
        {{ recommendationMode ? "复制文件" : "复制所选文件" }}
      </button>
      <button
        type="button"
        role="menuitem"
        :disabled="recommendationMode && actionPending"
        @click="emit('copyPaths'); emit('close')"
      >
        复制文件路径
      </button>
      <button
        type="button"
        role="menuitem"
        :disabled="exporting || (recommendationMode && actionPending)"
        @click="emit('export'); emit('close')"
      >
        {{ exporting ? "正在导出…" : "导出到文件夹…" }}
      </button>
      <template v-if="!recommendationMode">
        <hr />
        <button class="is-muted" type="button" role="menuitem" @click="emit('clear'); emit('close')">
          清除选择
        </button>
      </template>
    </div>
  </Teleport>
</template>

<style scoped>
.gallery-context-menu {
  --menu-bg: rgb(255 255 255 / 97%);
  --menu-border: rgb(204 210 224 / 92%);
  --menu-hover: #f1f1f1;
  --menu-text: var(--text, #171e2e);
  --menu-muted: var(--muted, #596478);
  --menu-separator: var(--border, #e1e6ef);
  position: fixed;
  z-index: 1000;
  display: grid;
  box-sizing: border-box;
  width: 238px;
  max-width: calc(100vw - 16px);
  max-height: calc(100vh - 16px);
  overflow-y: auto;
  padding: 8px;
  border: 1px solid var(--menu-border);
  border-radius: 14px;
  color: var(--menu-text);
  font-family: var(--font-ui, "Segoe UI", "Microsoft YaHei UI", sans-serif);
  font-size: 13px;
  background: var(--menu-bg);
  box-shadow: 0 20px 55px rgb(23 32 51 / 24%);
  backdrop-filter: blur(18px);
}

.gallery-context-menu header {
  display: grid;
  gap: 2px;
  padding: 8px 10px 9px;
}

.gallery-context-menu header strong {
  font-size: 14px;
}

.gallery-context-menu header small {
  color: var(--menu-muted);
  font-size: 12px;
}

.gallery-context-menu button {
  min-height: 38px;
  padding: 8px 10px;
  border: 0;
  border-radius: 9px;
  color: inherit;
  font: inherit;
  font-weight: 650;
  text-align: left;
  background: transparent;
  cursor: pointer;
}

.gallery-context-menu button:not(:disabled):hover,
.gallery-context-menu button:not(:disabled):focus-visible {
  outline: 0;
  color: var(--menu-text);
  background: var(--menu-hover);
}

.gallery-context-menu button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.gallery-context-menu button.is-muted {
  color: var(--menu-muted);
}

.gallery-context-menu button.feedback-positive:not(:disabled) {
  color: #21684d;
}

.gallery-context-menu button.feedback-negative:not(:disabled) {
  color: #a12c3c;
}

.gallery-context-menu hr {
  width: 100%;
  margin: 5px 0;
  border: 0;
  border-top: 1px solid var(--menu-separator);
}

@media (prefers-color-scheme: dark) {
  .gallery-context-menu {
    color-scheme: dark;
    --menu-bg: rgb(34 35 31 / 98%);
    --menu-border: rgb(255 255 255 / 14%);
    --menu-hover: #30312c;
    --menu-text: #f1f1eb;
    --menu-muted: #b1b2aa;
    --menu-separator: rgb(255 255 255 / 12%);
    box-shadow: 0 20px 55px rgb(0 0 0 / 46%);
  }

  .gallery-context-menu button.feedback-positive:not(:disabled) {
    color: #71d0a8;
  }

  .gallery-context-menu button.feedback-negative:not(:disabled) {
    color: #ff9aa7;
  }
}
</style>
