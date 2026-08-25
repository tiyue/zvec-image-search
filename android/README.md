# YaoLens for Android

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

## Galaxy Watch5 / Wear OS

The `wear` module is a standalone watch application; a phone is not required at
runtime. It defaults to `39.105.48.52:38522`, permits an editable IPv4 and port,
and uses the existing six-digit pairing flow confirmed on the computer.

Build and verify the watch APK:

```powershell
.\gradlew.bat :wear:testDebugUnitTest :wear:assembleDebug :wear:lintDebug
```

The APK is written to `wear/build/outputs/apk/debug/wear-debug.apk`. It can be
installed without Android Studio after enabling Developer options and Wireless
debugging on the watch:

```powershell
adb pair <watch-ip:pairing-port>
adb connect <watch-ip:debug-port>
adb -s <watch-ip:debug-port> install -r wear\build\outputs\apk\debug\wear-debug.apk
```

The pairing port shown by Wear OS is only for `adb pair`; use the separate
debug port shown by the watch for `adb connect` and `adb -s`. After installing,
open YaoLens on the watch, connect, and approve the displayed six-digit code in
the running Windows application. The watch requests six thumbnails per batch;
after they settle it sequentially caches all six originals without requiring a
tap. The full-screen viewer uses the watch's hardware/system back action,
supports bounded left/right navigation, 1x-5x pinch zoom with panning, and saves
the cached original to `Pictures/YaoLens` on long press. Connection,
recommendation, image, and save failures show both an error type and a concrete
reason.

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
