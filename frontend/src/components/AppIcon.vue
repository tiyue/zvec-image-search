<script setup lang="ts">
defineProps<{
  name: "logo" | "compose" | "search" | "tasks" | "tags" | "groups" | "learning" | "settings" | "panel" | "help" | "plus" | "arrow-up" | "sliders" | "chevron-down" | "image";
  size?: number;
}>();
</script>

<template>
  <svg
    class="app-icon"
    :width="size ?? 18"
    :height="size ?? 18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <template v-if="name === 'logo'">
      <path d="M12 3.2a4 4 0 0 1 3.6 2.2 4 4 0 0 1 4.4 4 4 4 0 0 1-1.2 2.9 4 4 0 0 1-2.2 5.3 4 4 0 0 1-5.3 1.2A4 4 0 0 1 6 16.6a4 4 0 0 1-1.2-5.3A4 4 0 0 1 7 5.4 4 4 0 0 1 12 3.2Z" />
      <path d="m8.4 9.3 3.8 2.2v4.4M15.6 14.7l-3.8-2.2V8.1" />
    </template>
    <template v-else-if="name === 'compose'">
      <path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" />
    </template>
    <template v-else-if="name === 'search'">
      <circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" />
    </template>
    <template v-else-if="name === 'tasks'">
      <ellipse cx="12" cy="5" rx="7.5" ry="2.5" /><path d="M4.5 5v6c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5V5M4.5 11v6c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5v-6" />
    </template>
    <template v-else-if="name === 'tags'">
      <path d="M20 13 13 20l-9-9V4h7Z" /><circle cx="8.5" cy="8.5" r="1" />
    </template>
    <template v-else-if="name === 'groups'">
      <rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" />
    </template>
    <template v-else-if="name === 'learning'">
      <path d="m3 10 9-5 9 5-9 5Z" /><path d="M7 12.5V17c2.7 2 7.3 2 10 0v-4.5M21 10v6" />
    </template>
    <template v-else-if="name === 'settings'">
      <circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z" />
    </template>
    <template v-else-if="name === 'panel'">
      <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" />
    </template>
    <template v-else-if="name === 'help'">
      <circle cx="12" cy="12" r="9" /><path d="M9.8 9a2.3 2.3 0 1 1 3.4 2c-.8.4-1.2 1-1.2 2M12 17h.01" />
    </template>
    <template v-else-if="name === 'plus'">
      <path d="M12 5v14M5 12h14" />
    </template>
    <template v-else-if="name === 'arrow-up'">
      <path d="m7 11 5-5 5 5M12 6v12" />
    </template>
    <template v-else-if="name === 'sliders'">
      <path d="M4 7h10M18 7h2M4 17h2M10 17h10M14 4v6M7 14v6" />
    </template>
    <template v-else-if="name === 'chevron-down'">
      <path d="m8 10 4 4 4-4" />
    </template>
    <template v-else>
      <rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="8.5" cy="9" r="1.5" /><path d="m21 15-5-5L5 20" />
    </template>
  </svg>
</template>
