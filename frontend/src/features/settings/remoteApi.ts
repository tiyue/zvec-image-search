import { requestJson } from "../../api/gateway";
import type {
  RemoteAccessApi,
  RemoteAccessImport,
  RemoteAccessStatusWire,
  RemoteAccessUpdate,
} from "./remoteTypes";

export const remoteAccessApi: RemoteAccessApi = {
  status: (signal) =>
    requestJson<RemoteAccessStatusWire>("api/remote-access", { signal }),
  importConfig: (body: RemoteAccessImport, signal) =>
    requestJson<RemoteAccessStatusWire>("api/remote-access/import", {
      method: "POST",
      body,
      signal,
    }),
  update: (body: RemoteAccessUpdate, signal) =>
    requestJson<RemoteAccessStatusWire>("api/remote-access", {
      method: "PUT",
      body,
      signal,
    }),
  start: (signal) =>
    requestJson<RemoteAccessStatusWire>("api/remote-access/start", {
      method: "POST",
      signal,
    }),
  stop: (signal) =>
    requestJson<RemoteAccessStatusWire>("api/remote-access/stop", {
      method: "POST",
      signal,
    }),
};
