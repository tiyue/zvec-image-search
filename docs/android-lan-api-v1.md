# Zvec Android LAN API v1

This contract defines the private-network boundary between the Windows Zvec
desktop process and the Android viewer. The existing backend remains bound to
loopback and is never exposed to the LAN.

Version 1 uses plain HTTP and does not provide end-to-end transport encryption.
It is a trusted-home-LAN preview, not a public or shared-network protocol.

## Discovery

- UDP port: `38521`
- Android request: UTF-8 `ZVEC_LAN_DISCOVER/1 <nonce>`
- Windows response: one compact JSON datagram:

```json
{
  "protocol": 1,
  "nonce": "client nonce",
  "instance_id": "stable random id",
  "name": "Zvec on DESKTOP",
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

Pairing is required once per Android installation. The desktop user must approve
the request. A six-digit comparison code is displayed on both devices.

```text
POST /api/v1/pair-requests
POST /api/v1/pair-requests/{pairing_id}/poll
```

Create request:

```json
{
  "device_id": "stable random Android installation id",
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
Windows stores only token and client-secret hashes. Android stores the bearer
token in Android Keystore-backed encrypted storage.

All following API calls require:

```text
Authorization: Bearer <device token>
```

Missing, invalid and revoked credentials return `401 Unauthorized`. Pairing
approval remains a loopback-only desktop action and is never exposed by this
LAN route table.

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

`top_k` has no Android-specific product cap; it remains a positive finite
integer accepted by the existing search contract. Result metadata is paged and
the Android client may browse every returned result.

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

Opaque `media_id` values never reveal an absolute Windows path. Android may run
multiple transfers concurrently; the app prioritizes visible items and
prefetches the next viewport while preserving all results.

`ETag` is the quoted lowercase SHA-256 recorded for the indexed source. `HEAD`
returns the same metadata headers as `GET` and no body. Only one byte range is
accepted; an invalid or unsatisfiable range returns `416` with
`Content-Range: bytes */<length>`.

## Session cleanup

`DELETE /api/v1/session` revokes the bearer token and releases that client's
temporary searches, media capabilities and uploaded query images. Cleanup is
internally idempotent. A later request with the revoked bearer receives `401`.
Revocation is attempted even if one temporary-resource cleanup step fails, so
a cleanup error never leaves an old token authorized.
The Windows credential store persists a hash-only deny journal before rewriting
its primary credential file. A failed rewrite therefore cannot resurrect the
revoked bearer after a process restart.

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
