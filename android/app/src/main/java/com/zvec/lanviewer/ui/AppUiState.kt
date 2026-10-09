package com.zvec.lanviewer.ui

import android.net.Uri
import com.zvec.lanviewer.data.local.ConnectionMode
import com.zvec.lanviewer.data.local.SavedFileRecord
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.model.DiscoveredServer
import com.zvec.lanviewer.data.model.LibraryDto
import com.zvec.lanviewer.data.model.OriginalMediaItem
import com.zvec.lanviewer.data.model.RecommendationAction
import com.zvec.lanviewer.data.model.RecommendationItem
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.SearchMode
import kotlinx.collections.immutable.PersistentList
import kotlinx.collections.immutable.persistentListOf

enum class ConnectionPhase {
    DISCOVERY,
    CONNECTING,
    PAIRING,
    READY,
}

data class QueryImageUiState(
    val uri: Uri,
    val displayName: String,
    val mimeType: String?,
    val queryImageId: String? = null,
    val uploading: Boolean = false,
    val bytesUploaded: Long = 0L,
    val totalBytes: Long? = null,
    val uploadError: String? = null,
    val uploadAttempt: Long = 0L,
)

enum class ViewerSource {
    SEARCH,
    RECOMMENDATIONS,
}

data class RecommendationUiState(
    val batchId: String? = null,
    val preloadedBatchId: String? = null,
    val shownBatchId: String? = null,
    val shownEventId: String? = null,
    val items: PersistentList<RecommendationItem> = persistentListOf(),
    val isLoading: Boolean = false,
    val errorMessage: String? = null,
    val partial: Boolean = false,
    val partialReason: String? = null,
    val reactions: Map<String, RecommendationAction> = emptyMap(),
    val pendingReactionItemIds: Set<String> = emptySet(),
    val actionEventIds: Map<String, String> = emptyMap(),
    val pendingActionKeys: Set<String> = emptySet(),
)

data class AppUiState(
    val phase: ConnectionPhase = ConnectionPhase.DISCOVERY,
    val isDiscovering: Boolean = false,
    val discoveredServers: List<DiscoveredServer> = emptyList(),
    val errorMessage: String? = null,
    val pairingCode: String? = null,
    val pairingSecondsRemaining: Long = 0L,
    val serverName: String? = null,
    val connectionDetail: String? = null,
    val connectionMode: ConnectionMode = ConnectionMode.LAN,
    val connectionOrigin: String? = null,
    val supportsRemoteMedia: Boolean = false,
    val lanEndpoint: String? = null,
    val remoteEndpoint: String? = null,
    val discoveryMode: ConnectionMode = ConnectionMode.LAN,
    val remoteRestoreFailed: Boolean = false,
    val viewerOriginalRequested: Boolean = false,
    val libraries: List<LibraryDto> = emptyList(),
    val selectedLibraryIds: Set<String> = emptySet(),
    val searchMode: SearchMode = SearchMode.TEXT,
    val searchText: String = "",
    val submittedSearchText: String? = null,
    val submittedSearchHasImage: Boolean = false,
    val topKText: String = "36",
    val queryImage: QueryImageUiState? = null,
    val isSearching: Boolean = false,
    val searchStatus: String? = null,
    val isLoadingNextPage: Boolean = false,
    val results: PersistentList<SearchItem> = persistentListOf(),
    val totalResults: Int = 0,
    val activeSearchId: String? = null,
    val nextPage: Int = 1,
    val searchPhase: SearchPhase = SearchPhase.IDLE,
    val searchAttempt: Long = 0L,
    val searchError: String? = null,
    val nextPageError: String? = null,
    val searchCompletionSignal: Long = 0L,
    val searchRestoreNotice: String? = null,
    val recommendations: RecommendationUiState = RecommendationUiState(),
    val viewerSource: ViewerSource? = null,
    val viewerIndex: Int? = null,
    val transferMessage: String? = null,
    val transferFraction: Float? = null,
    val transferAttempt: Long? = null,
    val savedFiles: List<SavedFileRecord> = emptyList(),
)

/**
 * What the viewer should render for one media item. `displayUrl == null` means only the thumbnail
 * placeholder is available; a remote desktop without `remote-v1` uses this to show an upgrade hint
 * and an explicit "view original" action instead of silently pulling the full-size file.
 */
internal data class ViewerImageTarget(
    val thumbnailUrl: String,
    val displayUrl: String?,
    val displayIsPreview: Boolean,
    val canViewOriginal: Boolean,
    val originalSizeBytes: Long?,
    val unavailableReason: String?,
)

internal fun ConnectionMode.label(): String = when (this) {
    ConnectionMode.LAN -> "局域网"
    ConnectionMode.REMOTE -> "远程"
}

internal fun AppUiState.viewerItems(): List<OriginalMediaItem> = when (viewerSource) {
    ViewerSource.SEARCH -> results
    ViewerSource.RECOMMENDATIONS -> recommendations.items
    null -> emptyList()
}

internal fun AppUiState.currentViewerItem(): OriginalMediaItem? =
    viewerIndex?.let(viewerItems()::getOrNull)

/**
 * The phone exposes only semantic and tag choices. Image and combined modes
 * are derived from the actual inputs so the UI cannot send contradictory
 * combinations to the desktop service.
 */
internal fun resolveSearchMode(visibleMode: SearchMode, hasText: Boolean, hasImage: Boolean): SearchMode = when {
    visibleMode == SearchMode.TAG -> SearchMode.TAG
    hasImage && hasText -> SearchMode.COMBINED
    hasImage -> SearchMode.IMAGE
    else -> SearchMode.TEXT
}

internal fun AppUiState.resolvedSearchMode(): SearchMode = resolveSearchMode(
    visibleMode = searchMode,
    hasText = searchText.isNotBlank(),
    hasImage = queryImage != null,
)

sealed interface AppEvent {
    data class Message(val text: String) : AppEvent
    data class Share(val uri: Uri, val mimeType: String) : AppEvent
}

internal fun AppUiState.afterPairingApproved(): AppUiState = copy(
    phase = ConnectionPhase.CONNECTING,
    pairingCode = null,
    pairingSecondsRemaining = 0L,
    connectionDetail = "电脑已批准，正在载入图库…",
    errorMessage = null,
)

/**
 * Applies a switch to another endpoint of the same authenticated profile. The query draft (text,
 * mode, top_k, libraries, image Uri) is preserved; the staged server results, recommendation batch,
 * viewer and old errors are cleared because they belong to the previous origin, and the user is
 * told to query again. A new origin always requires the query image to be re-uploaded.
 */
internal fun AppUiState.afterModeSwitched(connection: SavedConnection): AppUiState = copy(
    phase = ConnectionPhase.CONNECTING,
    isDiscovering = false,
    connectionMode = connection.mode,
    connectionOrigin = connection.baseUrl,
    lanEndpoint = connection.lanBaseUrl,
    remoteEndpoint = connection.remoteBaseUrl,
    supportsRemoteMedia = connection.supportsRemoteMedia,
    discoveryMode = connection.mode,
    errorMessage = null,
    pairingCode = null,
    pairingSecondsRemaining = 0L,
    connectionDetail = "正在切换到${connection.mode.label()}…",
    remoteRestoreFailed = false,
    results = persistentListOf(),
    totalResults = 0,
    activeSearchId = null,
    nextPage = 1,
    isSearching = false,
    isLoadingNextPage = false,
    searchStatus = null,
    searchError = null,
    nextPageError = null,
    searchPhase = SearchPhase.IDLE,
    searchRestoreNotice = MODE_SWITCH_NOTICE,
    submittedSearchText = null,
    submittedSearchHasImage = false,
    viewerSource = null,
    viewerIndex = null,
    viewerOriginalRequested = false,
    recommendations = RecommendationUiState(),
    // The staged query image id belongs to the previous origin, so it must be re-uploaded. The
    // selected Uri/name/mime are kept; the progress and status are reset and the error tells the
    // user why, which is what makes the existing retry button appear.
    queryImage = queryImage?.copy(
        queryImageId = null,
        uploading = false,
        bytesUploaded = 0L,
        totalBytes = null,
        uploadError = MODE_SWITCH_REUPLOAD_NOTICE,
    ),
)

internal const val MODE_SWITCH_NOTICE = "连接方式已切换，请重新查询"
internal const val MODE_SWITCH_REUPLOAD_NOTICE = "连接方式已切换，请重新上传查询图片"
