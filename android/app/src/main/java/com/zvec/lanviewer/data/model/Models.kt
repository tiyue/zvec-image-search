package com.zvec.lanviewer.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class DiscoveryResponse(
    val protocol: Int,
    val nonce: String,
    @SerialName("instance_id") val instanceId: String,
    val name: String,
    val host: String,
    val port: Int,
)

data class DiscoveredServer(
    val instanceId: String,
    val name: String,
    val host: String,
    val port: Int,
) {
    val stableKey: String get() = "$instanceId@$host:$port"
}

@Serializable
data class PairRequest(
    @SerialName("device_id") val deviceId: String,
    @SerialName("device_name") val deviceName: String,
    @SerialName("client_secret") val clientSecret: String,
)

@Serializable
data class PairStartResponse(
    @SerialName("pairing_id") val pairingId: String,
    @SerialName("comparison_code") val comparisonCode: String,
    @SerialName("expires_in_seconds") val expiresInSeconds: Long = 300,
    val status: String = "pending",
)

@Serializable
data class PairPollRequest(
    @SerialName("client_secret") val clientSecret: String,
)

@Serializable
data class PairPollResponse(
    val status: String,
    val token: String? = null,
)

@Serializable
data class StatusResponse(
    val protocol: Int,
    @SerialName("instance_id") val instanceId: String,
    val name: String,
)

@Serializable
data class LibraryDto(
    val id: String,
    val name: String,
    val count: Long? = null,
)

@Serializable
data class LibrariesResponse(
    val libraries: List<LibraryDto> = emptyList(),
)

@Serializable
data class QueryImageResponse(
    @SerialName("query_image_id") val queryImageId: String,
    val name: String? = null,
)

@Serializable
enum class SearchMode {
    @SerialName("text") TEXT,
    @SerialName("tag") TAG,
    @SerialName("image") IMAGE,
    @SerialName("combined") COMBINED,
}

@Serializable
data class SearchRequest(
    val mode: SearchMode,
    val text: String? = null,
    @SerialName("query_image_id") val queryImageId: String? = null,
    @SerialName("library_ids") val libraryIds: List<String> = emptyList(),
    @SerialName("top_k") val topK: Int,
)

@Serializable
data class SearchCreatedResponse(
    @SerialName("search_id") val searchId: String,
)

@Serializable
data class SearchItem(
    @SerialName("media_id") val mediaId: String,
    val score: Double? = null,
    val name: String = "",
    val width: Int? = null,
    val height: Int? = null,
    val tags: List<String> = emptyList(),
    @SerialName("library_id") val libraryId: String? = null,
    @SerialName("library_name") val libraryName: String? = null,
    @SerialName("content_type") val contentType: String? = null,
    @SerialName("size_bytes") val sizeBytes: Long? = null,
)

@Serializable
data class SearchPageResponse(
    @SerialName("search_id") val searchId: String,
    val page: Int,
    @SerialName("page_size") val pageSize: Int,
    val total: Int,
    val items: List<SearchItem> = emptyList(),
)

@Serializable
data class SearchPendingResponse(
    @SerialName("search_id") val searchId: String,
    val status: String = "running",
    @SerialName("retry_after_seconds") val retryAfterSeconds: Long = 1L,
)

sealed interface SearchPageResult {
    data class Ready(val page: SearchPageResponse) : SearchPageResult
    data class Pending(val status: String, val retryAfterSeconds: Long) : SearchPageResult
}

@Serializable
data class ErrorEnvelope(
    val error: ApiError,
)

@Serializable
data class ApiError(
    val code: String = "unknown_error",
    val message: String = "请求失败",
)

data class UploadProgress(
    val bytesWritten: Long,
    val contentLength: Long?,
) {
    val fraction: Float?
        get() = contentLength?.takeIf { it > 0L }
            ?.let { (bytesWritten.toDouble() / it.toDouble()).coerceIn(0.0, 1.0).toFloat() }
}

data class DownloadProgress(
    val bytesWritten: Long,
    val totalBytes: Long?,
) {
    val fraction: Float?
        get() = totalBytes?.takeIf { it > 0L }
            ?.let { (bytesWritten.toDouble() / it.toDouble()).coerceIn(0.0, 1.0).toFloat() }
}
