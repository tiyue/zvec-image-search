import { requestJson } from "../../api/gateway";
import type {
  LanAccessApi,
  LanAccessStatusWire,
  LanAccessUpdate,
} from "./lanTypes";

export const lanAccessApi: LanAccessApi = {
  status: (signal) => requestJson<LanAccessStatusWire>("api/lan-access", { signal }),
  update: (body: LanAccessUpdate, signal) =>
    requestJson<LanAccessStatusWire>("api/lan-access", {
      method: "PUT",
      body,
      signal,
    }),
  start: (signal) =>
    requestJson<LanAccessStatusWire>("api/lan-access/start", {
      method: "POST",
      signal,
    }),
  stop: (signal) =>
    requestJson<LanAccessStatusWire>("api/lan-access/stop", {
      method: "POST",
      signal,
    }),
  approve: (pairingId, signal) =>
    requestJson<LanAccessStatusWire>(
      `api/lan-access/pairings/${encodeURIComponent(pairingId)}/approve`,
      { method: "POST", signal },
    ),
  reject: (pairingId, signal) =>
    requestJson<LanAccessStatusWire>(
      `api/lan-access/pairings/${encodeURIComponent(pairingId)}/reject`,
      { method: "POST", signal },
    ),
  revokeDevice: (signal) =>
    requestJson<LanAccessStatusWire>("api/lan-access/device", {
      method: "DELETE",
      signal,
    }),
};
