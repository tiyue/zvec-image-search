package com.zvec.lanviewer.data.repository

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import androidx.core.content.FileProvider
import com.zvec.lanviewer.data.discovery.UdpDiscoveryService
import com.zvec.lanviewer.data.local.ConnectionStore
import com.zvec.lanviewer.data.local.InstallationStore
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.model.DiscoveredServer
import com.zvec.lanviewer.data.model.DownloadProgress
import com.zvec.lanviewer.data.model.LibrariesResponse
import com.zvec.lanviewer.data.model.OriginalMediaItem
import com.zvec.lanviewer.data.model.PairPollResponse
import com.zvec.lanviewer.data.model.PairRequest
import com.zvec.lanviewer.data.model.PairStartResponse
import com.zvec.lanviewer.data.model.QueryImageResponse
import com.zvec.lanviewer.data.model.RecommendationActionRequest
import com.zvec.lanviewer.data.model.RecommendationActionResponse
import com.zvec.lanviewer.data.model.RecommendationRequest
import com.zvec.lanviewer.data.model.RecommendationShownRequest
import com.zvec.lanviewer.data.model.RecommendationsResponse
import com.zvec.lanviewer.data.model.SearchCreatedResponse
import com.zvec.lanviewer.data.model.SearchPageResponse
import com.zvec.lanviewer.data.model.SearchPageResult
import com.zvec.lanviewer.data.model.SearchRequest
import com.zvec.lanviewer.data.model.StatusResponse
import com.zvec.lanviewer.data.model.UploadProgress
import com.zvec.lanviewer.data.network.LanApiClient
import com.zvec.lanviewer.data.network.ResumableDownloader
import com.zvec.lanviewer.data.security.SecureTokenStore
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64

data class PairingAttempt(
    val baseUrl: String,
    val pairingId: String,
    val comparisonCode: String,
    val expiresInSeconds: Long,
    internal val clientSecret: String,
)

internal class PairingTokenMissingException : IOException(
    "电脑已批准配对，但没有返回设备令牌（approved_token_missing）。请更新电脑端后重新配对",
)

class ZvecRepository(
    private val context: Context,
    private val discovery: UdpDiscoveryService,
    private val connectionStore: ConnectionStore,
    private val installationStore: InstallationStore,
    private val tokenStore: SecureTokenStore,
    private val api: LanApiClient,
    private val downloader: ResumableDownloader,
) {
    fun savedConnection(): SavedConnection? = connectionStore.read()

    fun hasToken(): Boolean = !tokenStore.read().isNullOrBlank()

    suspend fun discover(): List<DiscoveredServer> = discovery.discover()

    fun selectConnection(connection: SavedConnection) {
        val previous = connectionStore.read()
        // Discovery identity is untrusted and can be spoofed. A token survives only an exact-origin update.
        if (!canRetainBearer(previous, connection)) tokenStore.clear()
        connectionStore.write(connection)
    }

    suspend fun beginPairing(baseUrl: String): PairingAttempt {
        val secretBytes = ByteArray(32).also(SecureRandom()::nextBytes)
        val clientSecret = Base64.getUrlEncoder().withoutPadding().encodeToString(secretBytes)
        val response: PairStartResponse = api.startPairing(
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

    suspend fun pollPairing(attempt: PairingAttempt): String {
        val response = api.pollPairing(attempt.baseUrl, attempt.pairingId, attempt.clientSecret)
        return applyPairingPollResponse(response, tokenStore)
    }

    suspend fun status(): StatusResponse = api.status()

    suspend fun libraries(): LibrariesResponse = api.libraries()

    suspend fun recommendations(request: RecommendationRequest): RecommendationsResponse =
        api.recommendations(request)

    suspend fun markRecommendationsShown(batchId: String, request: RecommendationShownRequest) =
        api.markRecommendationsShown(batchId, request)

    suspend fun recordRecommendationAction(
        batchId: String,
        request: RecommendationActionRequest,
    ): RecommendationActionResponse = api.recordRecommendationAction(batchId, request)

    suspend fun uploadQueryImage(
        contentResolver: ContentResolver,
        uri: Uri,
        displayName: String,
        mimeType: String?,
        onProgress: (UploadProgress) -> Unit,
    ): QueryImageResponse = api.uploadQueryImage(
        contentResolver,
        uri,
        displayName,
        mimeType,
        onProgress,
    )

    suspend fun deleteQueryImage(queryImageId: String) = api.deleteQueryImage(queryImageId)

    suspend fun createSearch(request: SearchRequest): SearchCreatedResponse = api.createSearch(request)

    suspend fun searchPage(searchId: String, page: Int, pageSize: Int): SearchPageResult =
        api.searchPage(searchId, page, pageSize)

    suspend fun deleteSearch(searchId: String) = api.deleteSearch(searchId)

    fun mediaUrl(mediaId: String): String = api.mediaUrl(mediaId)

    suspend fun cacheOriginal(
        item: OriginalMediaItem,
        onProgress: (DownloadProgress) -> Unit = {},
    ): File {
        val extension = item.name.substringAfterLast('.', "bin")
            .lowercase()
            .takeIf { it.matches(Regex("[a-z0-9]{1,8}")) } ?: "bin"
        val safeId = MessageDigest.getInstance("SHA-256")
            .digest(item.mediaId.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
        val target = File(File(context.cacheDir, "shared-media"), "$safeId.$extension")
        return downloader.download(mediaUrl(item.mediaId), target, onProgress)
    }

    suspend fun copyOriginalTo(
        item: OriginalMediaItem,
        destination: Uri,
        onProgress: (DownloadProgress) -> Unit = {},
    ) {
        val cached = cacheOriginal(item, onProgress)
        val output = context.contentResolver.openOutputStream(destination, "w")
            ?: throw IOException("无法打开保存位置")
        output.use { sink -> cached.inputStream().use { source -> source.copyTo(sink, 256 * 1024) } }
    }

    suspend fun shareUri(item: OriginalMediaItem, onProgress: (DownloadProgress) -> Unit = {}): Uri {
        val cached = cacheOriginal(item, onProgress)
        return FileProvider.getUriForFile(context, "${context.packageName}.files", cached)
    }

    suspend fun disconnect(revokeServerSession: Boolean) {
        if (revokeServerSession && hasToken()) runCatching { api.deleteSession() }
        tokenStore.clear()
        connectionStore.clear()
    }
}

internal fun applyPairingPollResponse(
    response: PairPollResponse,
    tokenStore: SecureTokenStore,
): String = when (response.status.lowercase()) {
    "pending" -> "pending"
    "approved" -> {
        val token = response.token?.takeIf(String::isNotBlank)
            ?: throw PairingTokenMissingException()
        tokenStore.write(token)
        "approved"
    }
    "rejected", "denied" -> "rejected"
    "expired" -> "expired"
    else -> throw IOException("未知配对状态")
}

internal fun canRetainBearer(previous: SavedConnection?, next: SavedConnection): Boolean =
    previous?.baseUrl == next.baseUrl
