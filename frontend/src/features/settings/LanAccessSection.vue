<script setup lang="ts">
import type { LanAccessApi } from "./lanTypes";
import type { ToastKind } from "./types";
import { useLanAccess } from "./useLanAccess";

const props = withDefaults(defineProps<{
  api?: LanAccessApi;
  autoLoad?: boolean;
}>(), {
  autoLoad: true,
});

const emit = defineEmits<{
  toast: [title: string, message: string, kind: ToastKind];
}>();

const lan = useLanAccess(props.api, {
  onToast: (title, message, kind) => emit("toast", title, message, kind),
}, props.autoLoad);

function readableTime(value: string): string {
  if (!value) return "即将过期";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
}
</script>

<template>
  <section class="lan-card panel" aria-labelledby="lan-access-title">
    <header class="lan-heading">
      <div>
        <p class="eyebrow">ANDROID 局域网</p>
        <h2 id="lan-access-title">手机和平板访问</h2>
        <p>Android 只获得搜索与原图读取权限，不能修改设置、索引或删除图库文件。</p>
      </div>
      <span class="status-pill" :class="lan.status.value?.running ? 'success' : ''">
        {{ lan.status.value?.running ? "运行中" : "已关闭" }}
      </span>
    </header>

    <div class="lan-warning">
      <strong>仅限可信的家庭局域网</strong>
      <span>当前链路使用未加密 HTTP，原图和搜索请求会在局域网内直接传输。只应在你自己控制的家庭专用网络启用，不要用于公共、共享或访客 Wi-Fi。</span>
    </div>

    <form class="lan-form" @submit.prevent="lan.save">
      <label>
        <span>电脑显示名称</span>
        <input v-model="lan.draft.displayName" name="lan_display_name" maxlength="96" autocomplete="off" />
      </label>
      <label>
        <span>局域网 IPv4</span>
        <select
          v-if="lan.status.value?.availableHosts.length"
          v-model="lan.draft.bindHost"
          name="lan_bind_host"
        >
          <option
            v-for="host in lan.status.value.availableHosts"
            :key="host.address"
            :value="host.address"
          >
            {{ host.label }} · {{ host.address }}
          </option>
        </select>
        <input
          v-else
          v-model="lan.draft.bindHost"
          name="lan_bind_host"
          inputmode="decimal"
          placeholder="192.168.1.20"
          autocomplete="off"
        />
      </label>
      <label>
        <span>服务端口</span>
        <input v-model.number="lan.draft.port" name="lan_port" type="number" min="1" max="65535" />
      </label>
      <label class="lan-enabled">
        <input v-model="lan.draft.enabled" name="lan_enabled" type="checkbox" />
        <span><strong>允许 Android 局域网访问</strong><small>软件退出后服务会立即停止。</small></span>
      </label>
      <div class="lan-actions">
        <button class="button secondary" type="submit" :disabled="lan.busy.value">
          {{ lan.saving.value ? "保存中…" : "保存设置" }}
        </button>
        <button
          v-if="!lan.status.value?.running"
          class="button primary"
          type="button"
          :disabled="lan.busy.value || !lan.draft.enabled"
          @click="lan.start"
        >
          {{ lan.action.value === "start" ? "启动中…" : "启动局域网访问" }}
        </button>
        <button
          v-else
          class="button danger"
          type="button"
          :disabled="lan.busy.value"
          @click="lan.stop"
        >
          {{ lan.action.value === "stop" ? "停止中…" : "停止局域网访问" }}
        </button>
        <button class="button quiet" type="button" :disabled="lan.loading.value" @click="lan.load(false)">
          刷新
        </button>
      </div>
    </form>

    <dl v-if="lan.status.value?.running" class="lan-runtime">
      <div><dt>访问地址</dt><dd>{{ lan.status.value.address || "等待服务地址" }}</dd></div>
      <div><dt>自动发现</dt><dd>UDP {{ lan.status.value.discoveryPort }}</dd></div>
      <div><dt>图片方式</dt><dd>原图并发流式传输</dd></div>
    </dl>

    <section class="pairing-panel" aria-labelledby="pairing-title">
      <header>
        <div><h3 id="pairing-title">待确认设备</h3><p>Android 自动发现电脑后，必须在这里确认相同的6位验证码。</p></div>
        <span>{{ lan.status.value?.pendingPairings.length || 0 }} 个</span>
      </header>
      <p v-if="lan.status.value?.pendingPairings.length" class="pairing-safety" role="note">
        只有 Android 已经显示相同验证码时才能允许。手机没有显示验证码时不要批准，请等待手机自动重试。
      </p>
      <div v-if="lan.status.value?.pendingPairings.length" class="pairing-list">
        <article v-for="pairing in lan.status.value.pendingPairings" :key="pairing.id">
          <div>
            <strong>{{ pairing.deviceName }}</strong>
            <span>验证码 <b>{{ pairing.code }}</b> · {{ readableTime(pairing.expiresAt) }} 过期</span>
          </div>
          <div>
            <button class="button primary" type="button" :disabled="lan.busy.value" @click="lan.approve(pairing.id)">允许</button>
            <button class="button danger" type="button" :disabled="lan.busy.value" @click="lan.reject(pairing.id)">拒绝</button>
          </div>
        </article>
      </div>
      <p v-else class="lan-empty">当前没有等待确认的 Android 设备。</p>
    </section>

    <section v-if="lan.status.value?.device" class="paired-device" aria-labelledby="paired-device-title">
      <div>
        <p class="eyebrow">已配对设备</p>
        <h3 id="paired-device-title">{{ lan.status.value.device.deviceName }}</h3>
        <span>最近连接：{{ readableTime(lan.status.value.device.lastSeenAt) }}</span>
      </div>
      <button class="button danger" type="button" :disabled="lan.busy.value" @click="lan.revokeDevice">
        撤销访问
      </button>
    </section>

    <p v-if="lan.lastError.value" class="lan-error" role="alert">{{ lan.lastError.value }}</p>
    <p class="lan-status" aria-live="polite">{{ lan.status.value?.message }}</p>
  </section>
</template>

<style scoped>
.lan-card { display:grid;gap:14px;padding:18px;color:#17203a }
.lan-heading,.lan-heading>div,.pairing-panel header,.pairing-list article,.paired-device,.lan-actions{display:flex;align-items:center;justify-content:space-between;gap:12px}
.lan-heading{align-items:flex-start}.lan-heading>div{display:block}.lan-heading h2,.pairing-panel h3,.paired-device h3{margin:2px 0 0}.lan-heading p:not(.eyebrow),.pairing-panel header p{margin:6px 0 0;color:#68718a}
.eyebrow{margin:0;color:#6557e8;font-size:11px;font-weight:850;letter-spacing:.08em}.status-pill{padding:5px 9px;border-radius:999px;color:#6d7486;background:#eef1f6;font-size:11px;font-weight:800}.status-pill.success{color:#176b55;background:#e9f8f1}
.lan-warning{display:grid;gap:3px;padding:11px 13px;border:1px solid #f0c488;border-radius:12px;color:#74450f;background:#fff8ec}.lan-warning span{font-size:12px;line-height:1.5}
.lan-form{display:grid;grid-template-columns:1fr 1fr 150px;gap:11px}.lan-form>label:not(.lan-enabled){display:grid;gap:5px;font-size:12px;font-weight:750}.lan-form input,.lan-form select{width:100%;height:40px;border:1px solid #d5dae6;border-radius:10px;padding:0 10px;color:#17203a;background:#fff;font:inherit}.lan-enabled{display:flex;grid-column:1/-1;align-items:flex-start;gap:9px;padding:10px;border-radius:11px;background:#f7f8fc}.lan-enabled span{display:grid;gap:2px}.lan-enabled small{color:#747d90}.lan-actions{grid-column:1/-1;justify-content:flex-end;flex-wrap:wrap}
.lan-runtime{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0}.lan-runtime div{padding:10px 12px;border-radius:11px;background:#f3f4fb}.lan-runtime dt{color:#737d91;font-size:10px}.lan-runtime dd{margin:4px 0 0;overflow:hidden;font-size:12px;font-weight:800;text-overflow:ellipsis;white-space:nowrap}
.pairing-panel{display:grid;gap:10px;padding:13px;border:1px solid #e1e5ee;border-radius:13px}.pairing-panel header>span{font-size:11px;font-weight:800}.pairing-list{display:grid;gap:8px}.pairing-list article{padding:10px;border-radius:10px;background:#f8f7ff}.pairing-list article>div{display:flex;align-items:center;gap:8px}.pairing-list article>div:first-child{display:grid;gap:3px}.pairing-list span,.paired-device span{color:#727b8f;font-size:11px}.pairing-list b{color:#4f42bb;font-size:16px;letter-spacing:.14em}.lan-empty{margin:0;padding:14px;color:#798297;text-align:center;background:#f7f8fb;border-radius:10px}
.pairing-safety{margin:0;padding:9px 11px;border-radius:10px;color:#7a4a0b;background:#fff6e7;font-size:12px;line-height:1.5}
.paired-device{padding:12px 14px;border:1px solid #cbe9dd;border-radius:13px;background:#effaf5}.lan-error{margin:0;color:#a13d50}.lan-status{min-height:18px;margin:0;color:#6f7890;font-size:11px}
@media(max-width:900px){.lan-form,.lan-runtime{grid-template-columns:1fr}.pairing-list article,.paired-device,.lan-heading{align-items:stretch;flex-direction:column}.pairing-list article>div:last-child{justify-content:flex-end}}
</style>
