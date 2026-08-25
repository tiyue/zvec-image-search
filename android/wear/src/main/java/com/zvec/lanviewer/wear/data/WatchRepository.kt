package com.zvec.lanviewer.wear.data

import android.util.Base64
import java.io.IOException
import java.security.SecureRandom

data class PairingAttempt(
    val baseUrl: String,
    val pairingId: String,
    val comparisonCode: String,
    val expiresInSeconds: Long,
    internal val clientSecret: String,
)

enum class PairingStatus {
    PENDING,
    APPROVED,
    REJECTED,
    EXPIRED,
}

interface WatchRepository {
    fun savedConnection(): SavedConnection?
    fun hasToken(): Boolean
    fun selectConnection(connection: SavedConnection)
    fun clearToken()
    suspend fun beginPairing(baseUrl: String): PairingAttempt
    suspend fun pollPairing(attempt: PairingAttempt): PairingStatus
    suspend fun recommendations(requestId: String): RecommendationsResponse
    suspend fun markShown(batchId: String, eventId: String)
    suspend fun recordOpen(batchId: String, itemId: String, eventId: String)
    suspend fun recordSave(batchId: String, itemId: String, eventId: String)
}

class DefaultWatchRepository(
    private val connectionStore: ConnectionStore,
    private val installationStore: InstallationStore,
    private val tokenStore: TokenStore,
    private val api: ApiClient,
) : WatchRepository {
    override fun savedConnection(): SavedConnection? = connectionStore.read()

    override fun hasToken(): Boolean = !tokenStore.read().isNullOrBlank()

    override fun selectConnection(connection: SavedConnection) {
        if (connectionStore.read()?.baseUrl != connection.baseUrl) tokenStore.clear()
        connectionStore.write(connection)
    }

    override fun clearToken() = tokenStore.clear()

    override suspend fun beginPairing(baseUrl: String): PairingAttempt {
        val secret = ByteArray(32).also(SecureRandom()::nextBytes)
        val clientSecret = Base64.encodeToString(
            secret,
            Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING,
        )
        val response = api.startPairing(
            baseUrl,
            PairRequest(
                deviceId = installationStore.deviceId,
                deviceName = installationStore.deviceName,
                clientSecret = clientSecret,
            ),
        )
        return PairingAttempt(
            baseUrl = baseUrl,
            pairingId = response.pairingId,
            comparisonCode = response.comparisonCode,
            expiresInSeconds = response.expiresInSeconds,
            clientSecret = clientSecret,
        )
    }

    override suspend fun pollPairing(attempt: PairingAttempt): PairingStatus {
        val response = api.pollPairing(
            attempt.baseUrl,
            attempt.pairingId,
            attempt.clientSecret,
        )
        return when (response.status.lowercase()) {
            "pending" -> PairingStatus.PENDING
            "approved" -> {
                val token = response.token?.takeIf(String::isNotBlank)
                    ?: throw IOException("电脑已批准，但未返回连接凭据")
                tokenStore.write(token)
                PairingStatus.APPROVED
            }
            "rejected", "denied" -> PairingStatus.REJECTED
            "expired" -> PairingStatus.EXPIRED
            else -> throw IOException("未知配对状态")
        }
    }

    override suspend fun recommendations(requestId: String): RecommendationsResponse =
        api.watchRecommendations(RecommendationRequest(requestId))

    override suspend fun markShown(batchId: String, eventId: String) {
        api.markShown(batchId, RecommendationShownRequest(eventId))
    }

    override suspend fun recordOpen(batchId: String, itemId: String, eventId: String) {
        api.recordOpen(
            batchId,
            RecommendationActionRequest(
                eventId = eventId,
                itemId = itemId,
                action = "open",
                metadata = mapOf("surface" to "wear_original"),
            ),
        )
    }

    override suspend fun recordSave(batchId: String, itemId: String, eventId: String) {
        api.recordSave(
            batchId,
            RecommendationActionRequest(
                eventId = eventId,
                itemId = itemId,
                action = "export",
                metadata = mapOf("channel" to "wear_save"),
            ),
        )
    }
}
