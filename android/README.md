# Zvec LAN Viewer for Android

Native Android client for the private LAN API in `../docs/android-lan-api-v1.md`.

## Build

Requirements:

- JDK 17
- Android SDK Platform 34 and Build Tools 34.0.0

On Windows:

```powershell
$env:ANDROID_HOME="$env:LOCALAPPDATA\Android\Sdk"
.\gradlew.bat testDebugUnitTest assembleDebug
```

The debug APK is written to `app/build/outputs/apk/debug/app-debug.apk`. The
repository contains only the Gradle Wrapper and debug signing uses the normal
local Android debug key. No private release signing key is generated or stored.

## Implemented baseline

- UDP broadcast discovery on port 38521 plus manual private IPv4/port entry.
- Six-digit desktop-confirmed pairing and polling.
- Bearer token encrypted with an Android Keystore AES-GCM key; no HTTP logger is
  installed and pairing routes never receive an existing token.
- Status/library loading and text, tag, image, and combined search contracts.
- Async search polling (`202 queued/running`) followed by unbounded page
  traversal of the finite `top_k` selected by the user. Result pages contain
  100 items and accumulate in a structurally shared persistent list.
- Raw `ContentResolver` query-image streaming with progress and cancellation;
  unknown lengths use HTTP/1.1 chunked transfer and no app byte cap.
- A shared OkHttp dispatcher configured for 10 parallel requests by default.
- Original-file Coil gallery with disk cache, swipe viewer, pinch/double-tap
  zoom, save, and share.
- Explicit original download cache with `Range`, `If-Range`, `.part` resume,
  atomic completion, and progress reporting.

## Security boundary

Manual and discovered servers are restricted to literal RFC1918 IPv4 addresses
(`10/8`, `172.16/12`, `192.168/16`) over HTTP. Loopback, link-local, DNS names,
public addresses, HTTPS origins, URL credentials, paths, queries, and fragments
are rejected. A bearer token is retained only when the normalized origin is
identical; the untrusted discovery `instance_id` never authorizes token reuse.
