# YaoLens Android LAN API v1

This contract defines the private-network boundary between YaoLens Windows and
YaoLens Android. The existing backend remains bound to loopback and is never
exposed to the LAN.

Version 1 uses plain HTTP and does not provide end-to-end transport encryption.
It is intended only for trusted home LANs, not public or shared networks.

## Discovery

- UDP port: `38521`
- YaoLens Android request: UTF-8 `ZVEC_LAN_DISCOVER/1 <nonce>`
- YaoLens Windows response: one compact JSON datagram:

```json
{
  "protocol": 1,
  "nonce": "client nonce",
  "instance_id": "stable random id",
  "name": "YaoLens on DESKTOP",
  "host": "192.168.1.20",
  "port": 38522
}
```

The nonce must match. Discovery grants no authority. Manual host and port entry
is the required fallback when a router blocks broadcast traffic.

Both clients accept literal RFC1918 IPv4 addresses only: `10/8`, `172.16/12`
and `192.168/16`. Loopback, link-local, public addresses, DNS names, URL
credentials, paths, queries and fragments are rejected. The HTTP data plane
rejects browser `Origin`/`Sec-Fetch-*` requests and an unexpected `Host` header
to prevent a web page or DNS rebinding from reaching the pairing surface.

## Pairing

Pairing is required once per YaoLens Android installation. The YaoLens Windows
user must approve the request. A six-digit comparison code is displayed on both
devices.

```text
POST /api/v1/pair-requests
POST /api/v1/pair-requests/{pairing_id}/poll
```

Create request:

```json
{
  "device_id": "stable random YaoLens Android installation id",
  "device_name": "Galaxy Tab",
  "client_secret": "base64url encoded 256-bit random value"
}
```

Successful create response (`201 Created`):

```json
{
  "pairing_id": "opaque id",
  "comparison_code": "003721",
  "expires_in_seconds": 300,
  "status": "pending"
}
```

The comparison code is a string and leading zeroes are significant. Polling
uses an `application/json` body with a known `Content-Length`:

```json
{
  "client_secret": "the original client secret"
}
```

Poll responses use `pending`, `approved` or `rejected`. Only the first approved
poll contains `token`; concurrent polls cannot receive duplicate tokens. A
lost approved response requires a new pairing request rather than returning a
stored plaintext token.

The request contains a random `device_id`, a display `device_name`, and a
256-bit `client_secret`. Polling repeats the secret in the JSON body, never in a
URL. Once approved, the response returns a 256-bit bearer token exactly once.
YaoLens Windows stores only token and client-secret hashes. YaoLens Android
stores the bearer token in Android Keystore-backed encrypted storage.

All following API calls require:

```text
Authorization: Bearer <device token>
```

Missing, invalid and revoked credentials return `401 Unauthorized`. Pairing
approval remains a loopback-only YaoLens Windows action and is never exposed by
this LAN route table.

## Read-only/search API

```text
GET    /api/v1/status
GET    /api/v1/libraries
POST   /api/v1/query-images?name=<display name>
DELETE /api/v1/query-images/{query_image_id}
POST   /api/v1/searches
GET    /api/v1/searches/{search_id}?page=1&page_size=30
DELETE /api/v1/searches/{search_id}
GET    /api/v1/media/{media_id}/original
HEAD   /api/v1/media/{media_id}/original
GET    /api/v1/media/{media_id}/thumbnail
HEAD   /api/v1/media/{media_id}/thumbnail
DELETE /api/v1/session
```

The LAN route table must not expose settings, credentials, model configuration,
indexing, annotation, migration, cache cleanup, filesystem paths, native actions,
or deletion of library files.

### Query image upload

The request body is the original image byte stream. Both `Content-Length` and
HTTP/1.1 chunked transfer are accepted. There is no configured byte-size cap.
The server writes bounded chunks directly to a temporary file, reports storage
errors cleanly, supports cancellation by disconnect, and removes stale uploads
after completion or recovery. It never buffers the whole upload in memory.

Successful upload response (`201 Created`):

```json
{
  "query_image_id": "opaque temporary id"
}
```

### Search request

```json
{
  "mode": "text|tag|image|combined",
  "text": "optional text",
  "query_image_id": "optional uploaded image id",
  "library_ids": ["optional library ids"],
  "top_k": 100
}
```

`top_k` has no YaoLens Android-specific product cap; it remains a positive finite
integer accepted by the existing search contract. Result metadata is paged and
the YaoLens Android client may browse every returned result.

Search creation returns `202 Accepted`:

```json
{
  "search_id": "opaque search id"
}
```

The search is asynchronous. While a requested page is not ready, the page
endpoint returns `202 Accepted`, a `Retry-After` header and:

```json
{
  "search_id": "opaque search id",
  "status": "queued|running",
  "retry_after_seconds": 1
}
```

Once complete it returns `200 OK`:

```json
{
  "search_id": "opaque search id",
  "page": 1,
  "page_size": 30,
  "total": 120,
  "items": [
    {
      "media_id": "client-and-search-scoped capability",
      "name": "image.jpg",
      "score": 0.91,
      "tags": ["原神", "雷电将军"],
      "library_id": "library-id",
      "library_name": "人物图库",
      "content_type": "image/jpeg",
      "size_bytes": 1234567
    }
  ]
}
```

`page_size` is an internal transfer page size, not a total-result limit.
Deleting a search invalidates all media capabilities created for that client
and search.

### Original media

The media endpoint streams the indexed source file without loading it into
memory and supports one HTTP byte range per request:

- `200 OK` for a full response
- `206 Partial Content` with `Content-Range`
- `304 Not Modified` with the indexed SHA-256 ETag
- `416 Range Not Satisfiable`
- `Accept-Ranges: bytes`

Opaque `media_id` values never reveal an absolute Windows path. YaoLens Android
may run multiple transfers concurrently; the app prioritizes visible items and
prefetches the next viewport while preserving all results.

For a search capability, `ETag` is the quoted lowercase SHA-256 recorded for
the indexed source. Creating a recommendation capability does not read the
source file solely to calculate this digest; the first `/original` request
calculates and caches the same strong SHA-256 for the exact stable file version.
`HEAD` returns the same metadata headers as `GET` and no body. Only one byte
range is accepted; an invalid or unsatisfiable range returns `416` with
`Content-Range: bytes */<length>`.

### Thumbnail media

The authenticated thumbnail route returns the shared ImageRegistry 640-pixel
thumbnail rather than the source file. It has the same client/session owner
capability and revocation checks as `/original`; a media ID owned by another
session, a changed source version, or a revoked capability returns the same
safe `404` without revealing a path.

Successful responses include an image `Content-Type`, exact `Content-Length`,
a quoted opaque `ETag`, `Cache-Control: private, max-age=300`, and
`X-Content-Type-Options: nosniff`. `If-None-Match` may return `304`; `HEAD`
returns the same metadata without a body. The payload is size bounded and uses
the existing persistent cache, per-image single-flight and bounded renderer.
The route never returns a path, token, internal registry ID or original bytes.
Search result payloads keep their existing media URL behavior; TC-007 changes
only recommendation items so that `thumbnail_url` uses `/thumbnail` while
`preview_url` remains `/original`.

## Recommendations

The authenticated recommendation routes are:

```text
POST /api/v1/recommendations
POST /api/v1/watch/recommendations
POST /api/v1/recommendations/{batch_id}/shown
POST /api/v1/recommendations/{batch_id}/actions
```

Batch creation accepts exactly one opaque request identifier:

```json
{
  "request_id": "installation-scoped idempotency id"
}
```

`POST /api/v1/recommendations` keeps the existing 15-item target and
`quality=5`, `low_exposure=6`, `random=4` quotas. The standalone Wear OS client
uses `POST /api/v1/watch/recommendations`; it accepts the same exact request
body and returns the same response shape with a five-item target and
`quality=2`, `low_exposure=2`, `random=1`. A partial batch remains possible
when the eligible library cannot supply five distinct items. The Wear route
does not change the desktop or phone Android recommendation contract.

A successful response includes the device-scoped batch and exposure metadata,
plus the computer-wide explicit preference state:

```json
{
  "request_id": "request id",
  "batch_id": "opaque batch id",
  "count": 15,
  "partial": false,
  "partial_reason": "",
  "quota_degraded": false,
  "history_window": 240,
  "quota": {
    "quality": 5,
    "low_exposure": 6,
    "random": 4
  },
  "diversity": {
    "applied": true,
    "reason": null,
    "missing_vectors": 0,
    "vector_space": {
      "model": "configured embedding model",
      "dimension": 1024,
      "metric": "COSINE"
    }
  },
  "personalization": {
    "applied": true,
    "effective_count": 12,
    "reason": null
  },
  "items": [
    {
      "item_id": "opaque item id",
      "media_id": "client-scoped media capability",
      "name": "image.jpg",
      "tags": ["原神", "雷电将军"],
      "library_id": "library-id",
      "library_name": "人物图库",
      "content_type": "image/jpeg",
      "size_bytes": 1234567,
      "width": 1800,
      "height": 2400,
      "bucket": "quality",
      "preference": "like",
      "thumbnail_url": "http://private-host/api/v1/media/.../thumbnail",
      "preview_url": "http://private-host/api/v1/media/.../original"
    }
  ]
}
```

New batches use only `quality`, `low_exposure`, and `random`. An idempotent
replay of a batch persisted by an earlier version may still contain an item
with `"bucket": "recent"`; clients must keep that item readable and present it
as a generic recommendation rather than as a recently added source.

`preference` is the latest successfully recorded `like` or `dislike` for the
item SHA-256 across the desktop viewer and every authorized Android viewer. It
may be `null` when no explicit preference exists. Conflicts use the largest
computer-side event sequence. New batches exclude every explicitly preferred
original image; replaying an existing idempotent batch still returns its current
shared preference.

`personalization.reason` is `null` when personalization was applied. Safe
non-applied values are `insufficient_preferences`, `vectors_unavailable`,
`incompatible_vector_spaces`, and `replayed`. `replayed` means that an existing
idempotent batch was returned without claiming that its original ranking was
recomputed. `effective_count` counts final explicit preferences with usable
existing vectors; at least 10 are required. No response exposes another device
identifier or a complete feedback history. The vector profile scans at most the
latest 4096 explicit preference events, keeps the first event per SHA-256 in
that newest-first window, and loads vectors for at most 256 images. Older
preferences outside an unusually duplicate-heavy profile window still retain
their exact shared state and remain excluded from new batches.

The shown request accepts exactly `{ "event_id": "..." }`. Android starts all
batch thumbnail loads together and may commit the batch after its fixed first
four thumbnails are ready. At that point the grid containing all 15 image slots
is visible and the batch counts as displayed, so shown is submitted exactly
once while the remaining thumbnails settle in the background. A generated or
fully preloaded next batch that has not been committed is never shown and does
not affect exposure or the 240-item history. Batch, shown history, and exposure
counts remain isolated by Android installation and are never merged with
desktop browsing history.

Wear starts all five thumbnail loads together and may commit the batch after
any two thumbnails are ready. It keeps the current five-item batch visible
while a replacement is slow, prepares at most one next batch, and retries a
failed replacement without clearing the current grid. After the thumbnails
settle, Wear sequentially preloads all five `/original` resources into the same
Coil disk cache without requiring a tap. Opening an item cancels that background
queue and gives the selected cached original priority. The original viewer has
no on-screen back control, retains system/hardware back, and supports bounded
1x-5x pinch zoom plus panning. The Wear client rebases returned media paths onto
its currently selected IPv4 and port so thumbnail and original requests use the
same FRP endpoint as batch creation.

After the current visible batch is shown and all its thumbnails settle, the
client may prepare at most one next batch in memory. Consuming that batch does
not create a duplicate request. Leaving the Recommendation tab, stopping the
Activity, disconnecting or re-pairing discards it. A successful explicit
preference change also discards it; open/export, a repeated final preference and
idempotent replay do not. A cross-device preference change while the page stays
continuously visible may affect at most that one already generated snapshot;
v1 adds no polling endpoint for this boundary.

The action request contains `event_id`, `item_id`, and one of `open`, `like`,
`dislike`, or `export`. `export` may also contain bounded string metadata. Only
`like/dislike` change the shared explicit preference. Identical event IDs are
idempotent; a failed request is not visible as a successful shared preference.
The success body returns `ok`, `event_id`, `recorded`, and the current
server-final `preference` (`like`, `dislike`, or `null`). Clients must use that
returned preference instead of assuming that an idempotently replayed old event
is still the latest cross-device event.

## Session cleanup

`DELETE /api/v1/session` revokes the bearer token and releases that client's
temporary searches, media capabilities and uploaded query images. Cleanup is
internally idempotent. A later request with the revoked bearer receives `401`.
Revocation is attempted even if one temporary-resource cleanup step fails, so
a cleanup error never leaves an old token authorized.
The YaoLens Windows credential store persists a hash-only deny journal before
rewriting its primary credential file. A failed rewrite therefore cannot
resurrect the revoked bearer after a process restart.

## Error envelope

```json
{
  "error": {
    "code": "stable_machine_code",
    "message": "safe user-facing text",
    "details": {}
  }
}
```

No error may contain credentials, bearer tokens, absolute paths, image bytes, or
unbounded stack traces.
