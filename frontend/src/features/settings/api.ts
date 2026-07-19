import type {
  CredentialsResponse,
  LibraryCreate,
  LibraryUpdate,
  ModelSettingsWire,
  SettingsApi,
  SettingsResponse,
} from "./types";
import { requestJson } from "../../api/gateway";

export { GatewayError as SettingsApiError } from "../../api/gateway";

export const settingsApi: SettingsApi = {
  settings: (signal) => requestJson<SettingsResponse>("api/settings", { signal }),
  addLibrary: (body: LibraryCreate, signal?: AbortSignal) =>
    requestJson<SettingsResponse>("api/libraries", {
      method: "POST",
      body,
      signal,
    }),
  updateLibrary: (libraryId: string, body: LibraryUpdate, signal?: AbortSignal) =>
    requestJson<SettingsResponse>(`api/libraries/${encodeURIComponent(libraryId)}`, {
      method: "PUT",
      body,
      signal,
    }),
  updateModels: (body, signal) =>
    requestJson<ModelSettingsWire>("api/models", { method: "PUT", body, signal }),
  saveCredentials: (apiKey, signal) =>
    requestJson<CredentialsResponse>("api/credentials", {
      method: "POST",
      body: { api_key: apiKey },
      signal,
    }),
  deleteCredentials: (signal) =>
    requestJson<CredentialsResponse>("api/credentials", { method: "DELETE", signal }),
};
