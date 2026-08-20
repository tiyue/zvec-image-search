package com.zvec.lanviewer.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

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
    @SerialName("expires_at") val expiresAt: String? = null,
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

interface OriginalMediaItem {
    val mediaId: String
    val score: Double?
    val name: String
    val width: Int?
    val height: Int?
    val tags: List<String>
    val libraryId: String?
    val libraryName: String?
    val contentType: String?
    val sizeBytes: Long?
}

@Serializable
data class SearchItem(
    @SerialName("media_id") override val mediaId: String,
    override val score: Double? = null,
    override val name: String = "",
    override val width: Int? = null,
    override val height: Int? = null,
    override val tags: List<String> = emptyList(),
    @SerialName("library_id") override val libraryId: String? = null,
    @SerialName("library_name") override val libraryName: String? = null,
    @SerialName("content_type") override val contentType: String? = null,
    @SerialName("size_bytes") override val sizeBytes: Long? = null,
) : OriginalMediaItem

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
data class RecommendationRequest(
    @SerialName("request_id") val requestId: String,
)

@Serializable
data class RecommendationQuota(
    val quality: Int = 0,
    val recent: Int = 0,
    @SerialName("low_exposure") val lowExposure: Int = 0,
    val random: Int = 0,
)

@Serializable
data class RecommendationDiversity(
    val applied: Boolean = false,
    val reason: String? = null,
    @SerialName("missing_vectors") val missingVectors: Int = 0,
    @SerialName("vector_space") val vectorSpace: JsonElement? = null,
)

@Serializable
enum class RecommendationPreference {
    @SerialName("like") LIKE,
    @SerialName("dislike") DISLIKE,
}

@Serializable
data class RecommendationPersonalization(
    val applied: Boolean = false,
    @SerialName("effective_count") val effectiveCount: Int = 0,
    val reason: String? = null,
)

@Serializable
data class RecommendationItem(
    @SerialName("item_id") val itemId: String,
    @SerialName("media_id") override val mediaId: String,
    override val name: String,
    override val width: Int,
    override val height: Int,
    override val tags: List<String>,
    @SerialName("library_id") override val libraryId: String,
    @SerialName("library_name") override val libraryName: String,
    @SerialName("content_type") override val contentType: String,
    @SerialName("size_bytes") override val sizeBytes: Long,
    val bucket: String,
    @SerialName("thumbnail_url") val thumbnailUrl: String,
    @SerialName("preview_url") val previewUrl: String,
    override val score: Double? = null,
    val preference: RecommendationPreference? = null,
) : OriginalMediaItem

@Serializable
data class RecommendationsResponse(
    @SerialName("request_id") val requestId: String,
    @SerialName("batch_id") val batchId: String,
    val count: Int,
    val partial: Boolean,
    @SerialName("partial_reason") val partialReason: String,
    @SerialName("quota_degraded") val quotaDegraded: Boolean,
    @SerialName("history_window") val historyWindow: Int,
    val items: List<RecommendationItem>,
    val quota: RecommendationQuota,
    val diversity: RecommendationDiversity,
    val personalization: RecommendationPersonalization = RecommendationPersonalization(),
)

@Serializable
data class RecommendationShownRequest(
    @SerialName("event_id") val eventId: String,
)

@Serializable
enum class RecommendationAction {
    @SerialName("open") OPEN,
    @SerialName("like") LIKE,
    @SerialName("export") EXPORT,
    @SerialName("dislike") DISLIKE,
}

@Serializable
data class RecommendationActionRequest(
    @SerialName("event_id") val eventId: String,
    @SerialName("item_id") val itemId: String,
    val action: RecommendationAction,
    val metadata: Map<String, String>? = null,
)

data class RecommendationActionResponse(
    val preferenceProvided: Boolean,
    val preference: RecommendationPreference?,
)

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
