export interface LanHostOptionWire {
  address?: unknown;
  label?: unknown;
}

export interface LanPairingWire {
  id?: unknown;
  device_name?: unknown;
  code?: unknown;
  expires_at?: unknown;
  status?: unknown;
}

export interface LanDeviceWire {
  device_id?: unknown;
  device_name?: unknown;
  paired_at?: unknown;
  last_seen_at?: unknown;
}

export interface LanAccessStatusWire {
  enabled?: unknown;
  running?: unknown;
  bind_host?: unknown;
  port?: unknown;
  display_name?: unknown;
  discovery_port?: unknown;
  address?: unknown;
  available_hosts?: unknown;
  pending_pairings?: unknown;
  device?: unknown;
  message?: unknown;
}

export interface LanHostOption {
  address: string;
  label: string;
}

export interface LanPairing {
  id: string;
  deviceName: string;
  code: string;
  expiresAt: string;
  status: string;
}

export interface LanDevice {
  deviceId: string;
  deviceName: string;
  pairedAt: string;
  lastSeenAt: string;
}

export interface LanAccessStatus {
  enabled: boolean;
  running: boolean;
  bindHost: string;
  port: number;
  displayName: string;
  discoveryPort: number;
  address: string;
  availableHosts: LanHostOption[];
  pendingPairings: LanPairing[];
  device: LanDevice | null;
  message: string;
}

export interface LanAccessUpdate {
  enabled: boolean;
  bind_host: string;
  port: number;
  display_name: string;
}

export interface LanAccessApi {
  status(signal?: AbortSignal): Promise<LanAccessStatusWire>;
  update(body: LanAccessUpdate, signal?: AbortSignal): Promise<LanAccessStatusWire>;
  start(signal?: AbortSignal): Promise<LanAccessStatusWire>;
  stop(signal?: AbortSignal): Promise<LanAccessStatusWire>;
  approve(pairingId: string, signal?: AbortSignal): Promise<LanAccessStatusWire>;
  reject(pairingId: string, signal?: AbortSignal): Promise<LanAccessStatusWire>;
  revokeDevice(signal?: AbortSignal): Promise<LanAccessStatusWire>;
}
