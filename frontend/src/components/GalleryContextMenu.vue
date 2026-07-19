<script setup lang="ts">
defineProps<{
  visible: boolean;
  x: number;
  y: number;
  selectionCount: number;
  exporting: boolean;
}>();

const emit = defineEmits<{
  open: [];
  reveal: [];
  copyImage: [];
  copyFiles: [];
  copyPaths: [];
  export: [];
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
      aria-label="图片操作"
      :style="{ left: `${x}px`, top: `${y}px` }"
      @click.stop
      @contextmenu.prevent
    >
      <header>
        <strong>已选择 {{ selectionCount }} 张</strong>
        <small>支持 Ctrl / Shift 跨页保留选择</small>
      </header>
      <button type="button" role="menuitem" @click="emit('open'); emit('close')">系统打开</button>
      <button type="button" role="menuitem" @click="emit('reveal'); emit('close')">所在文件夹</button>
      <hr />
      <button
        type="button"
        role="menuitem"
        :disabled="selectionCount !== 1"
        @click="emit('copyImage'); emit('close')"
      >
        复制图片
      </button>
      <button type="button" role="menuitem" @click="emit('copyFiles'); emit('close')">
        复制所选文件
      </button>
      <button type="button" role="menuitem" @click="emit('copyPaths'); emit('close')">
        复制文件路径
      </button>
      <button
        type="button"
        role="menuitem"
        :disabled="exporting"
        @click="emit('export'); emit('close')"
      >
        {{ exporting ? "正在导出…" : "导出到文件夹…" }}
      </button>
      <hr />
      <button class="is-muted" type="button" role="menuitem" @click="emit('clear'); emit('close')">
        清除选择
      </button>
    </div>
  </Teleport>
</template>

<style scoped>
.gallery-context-menu {
  position: fixed;
  z-index: 1000;
  display: grid;
  width: 238px;
  padding: 8px;
  border: 1px solid rgb(204 210 224 / 92%);
  border-radius: 14px;
  color: var(--text, #171e2e);
  font-family: var(--font-ui, "Segoe UI", "Microsoft YaHei UI", sans-serif);
  font-size: 13px;
  background: rgb(255 255 255 / 97%);
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
  color: var(--muted, #596478);
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
  color: var(--brand-strong, #443ab7);
  background: var(--brand-soft, #f0efff);
}

.gallery-context-menu button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.gallery-context-menu button.is-muted {
  color: var(--muted, #596478);
}

.gallery-context-menu hr {
  width: 100%;
  margin: 5px 0;
  border: 0;
  border-top: 1px solid var(--border, #e1e6ef);
}
</style>
