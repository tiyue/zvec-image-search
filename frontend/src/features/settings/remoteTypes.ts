export interface RemoteAccessStatusWire {
  configured?: unknown;
  enabled?: unknown;
  state?: unknown;
  message?: unknown;
  public_url?: unknown;
  server_host?: unknown;
  server_port?: unknown;
  remote_port?: unknown;
}

/**
 * Mirrors the owned FRPC runtime states. ``connected`` means the proxy is
 * registered, not merely that a child process exists.
 */
export type RemoteConnectionState =
  | "unconfigured"
  | "disabled"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "error";

export interface RemoteAccessStatus {
  configured: boolean;
  enabled: boolean;
  state: RemoteConnectionState;
  message: string;
  publicUrl: string;
  serverHost: string;
  serverPort: number | null;
  remotePort: number | null;
}

export interface RemoteAccessImport {
  source_path: string;
}

export interface RemoteAccessUpdate {
  enabled: boolean;
}

export interface RemoteAccessApi {
  status(signal?: AbortSignal): Promise<RemoteAccessStatusWire>;
  importConfig(
    body: RemoteAccessImport,
    signal?: AbortSignal,
  ): Promise<RemoteAccessStatusWire>;
  update(
    body: RemoteAccessUpdate,
    signal?: AbortSignal,
  ): Promise<RemoteAccessStatusWire>;
  start(signal?: AbortSignal): Promise<RemoteAccessStatusWire>;
  stop(signal?: AbortSignal): Promise<RemoteAccessStatusWire>;
}
