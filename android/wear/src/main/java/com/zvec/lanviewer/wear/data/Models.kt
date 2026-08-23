package com.zvec.lanviewer.wear.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

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
data class RecommendationRequest(
    @SerialName("request_id") val requestId: String,
)

@Serializable
data class RecommendationItem(
    @SerialName("item_id") val itemId: String,
    @SerialName("media_id") val mediaId: String,
    val name: String = "",
    val width: Int? = null,
    val height: Int? = null,
    val bucket: String = "random",
    @SerialName("thumbnail_url") val thumbnailUrl: String,
    @SerialName("preview_url") val previewUrl: String,
)

@Serializable
data class RecommendationsResponse(
    @SerialName("request_id") val requestId: String,
    @SerialName("batch_id") val batchId: String,
    val count: Int = 0,
    val partial: Boolean = false,
    @SerialName("partial_reason") val partialReason: String = "",
    val items: List<RecommendationItem> = emptyList(),
)

@Serializable
data class RecommendationShownRequest(
    @SerialName("event_id") val eventId: String,
)

@Serializable
data class RecommendationActionRequest(
    @SerialName("event_id") val eventId: String,
    @SerialName("item_id") val itemId: String,
    val action: String,
    val metadata: Map<String, String>? = null,
)

@Serializable
data class ApiErrorEnvelope(
    val error: ApiErrorPayload? = null,
)

@Serializable
data class ApiErrorPayload(
    val code: String = "",
    val message: String = "",
)
