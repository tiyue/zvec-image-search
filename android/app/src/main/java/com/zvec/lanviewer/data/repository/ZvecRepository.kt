package com.zvec.lanviewer.data.repository

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import androidx.core.content.FileProvider
import com.zvec.lanviewer.data.discovery.UdpDiscoveryService
import com.zvec.lanviewer.data.local.ConnectionStore
import com.zvec.lanviewer.data.local.ConnectionMode
import com.zvec.lanviewer.data.local.InstallationStore
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.local.ServerAddress
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

    /**
     * Registers the endpoints the desktop authenticated for this device. The aliases are trusted
     * only because this runs after a Bearer-authenticated `/status` on the current origin and the
     * instance id matches the already-saved one (an initial null id is allowed to bind the
     * authenticated id). A mismatched id never overwrites the saved profile, and when the status
     * advertises endpoints that do not cover the current authenticated origin the previous profile,
     * aliases, mode and token are all preserved.
     */
    fun registerAuthenticatedEndpoints(status: StatusResponse) {
        val saved = connectionStore.read() ?: return
        val existingId = saved.instanceId
        if (existingId != null && existingId != status.instanceId) return
        val advertised = status.connectionEndpoints
        val advertisedLan = advertised?.lan?.let(ServerAddress::parseOrigin)
        val advertisedRemote = advertised?.remote?.let(ServerAddress::parseOrigin)
        val current = ServerAddress.parseOrigin(saved.baseUrl)
        if (advertised != null) {
            // A status is only this profile's if it advertises the origin this device is currently
            // authenticated on, or an alias the device already authenticated earlier. A mismatched
            // endpoint list must not move the aliases, mode or token to another address.
            val trusted = listOfNotNull(
                advertisedLan,
                advertisedRemote,
                saved.lanBaseUrl?.let(ServerAddress::parseOrigin),
                saved.remoteBaseUrl?.let(ServerAddress::parseOrigin),
            ).toSet()
            if (current == null || current !in trusted) return
        }
        val effectiveLan = advertisedLan ?: saved.lanBaseUrl
        val effectiveRemote = advertisedRemote ?: saved.remoteBaseUrl
        val mode = when {
            current != null && current == effectiveRemote -> ConnectionMode.REMOTE
            current != null && current == effectiveLan -> ConnectionMode.LAN
            // No advertised alias matches this origin (for example a manually chosen or
            // VPN/private remote entry): keep the user's explicit mode instead of reclassifying.
            else -> saved.mode
        }
        val updated = saved.copy(
            instanceId = saved.instanceId ?: status.instanceId,
            displayName = status.name,
            mode = mode,
            lanBaseUrl = effectiveLan,
            remoteBaseUrl = effectiveRemote,
            supportsRemoteMedia = status.mediaProfiles.contains(REMOTE_MEDIA_PROFILE),
        )
        if (!canRetainBearer(saved, updated)) tokenStore.clear()
        connectionStore.write(updated)
    }

    /**
     * Moves the active origin to an endpoint this profile already authenticated, keeping the aliases
     * and the bearer token. Returns null when the requested mode has no registered endpoint or when
     * the destination is not part of the trusted profile, so the caller never reuses a token for an
     * arbitrary address.
     */
    fun switchRegisteredEndpoint(mode: ConnectionMode): SavedConnection? {
        val saved = connectionStore.read() ?: return null
        val target = saved.endpointFor(mode)?.takeIf { it.isNotBlank() } ?: return null
        val next = saved.copy(baseUrl = target, mode = mode)
        if (!canRetainBearer(saved, next)) return null
        connectionStore.write(next)
        return next
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

    fun thumbnailUrl(mediaId: String): String = api.thumbnailUrl(mediaId)

    fun previewUrl(mediaId: String): String = api.previewUrl(mediaId)

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
        copyOriginalFile(cached, { context.contentResolver.openOutputStream(destination, "w") })
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

    companion object {
        const val REMOTE_MEDIA_PROFILE = "remote-v1"
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

internal fun classifyOriginMode(origin: String): ConnectionMode {
    val host = origin.substringAfter("://", origin).substringBefore('/').substringBefore(':')
    return if (ServerAddress.isValidIpv4(host) && !ServerAddress.isPrivateIpv4(host)) {
        ConnectionMode.REMOTE
    } else {
        ConnectionMode.LAN
    }
}

/**
 * A bearer survives an exact-origin profile update (metadata/alias refresh) or a move to an
 * endpoint the *previous authenticated profile* already registered under the same instance id.
 * The next profile's own aliases are never consulted, so a forged or spoofed profile cannot move
 * the token to a new origin.
 */
internal fun canRetainBearer(previous: SavedConnection?, next: SavedConnection): Boolean {
    if (previous == null) return false
    if (previous.baseUrl == next.baseUrl) return true
    val instanceId = previous.instanceId
    if (instanceId.isNullOrBlank() || next.instanceId != instanceId) return false
    return next.baseUrl == previous.lanBaseUrl || next.baseUrl == previous.remoteBaseUrl
}
