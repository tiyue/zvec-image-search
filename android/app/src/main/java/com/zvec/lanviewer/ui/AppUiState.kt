package com.zvec.lanviewer.ui

import android.net.Uri
import com.zvec.lanviewer.data.local.SavedFileRecord
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
    val libraries: List<LibraryDto> = emptyList(),
    val selectedLibraryIds: Set<String> = emptySet(),
    val searchMode: SearchMode = SearchMode.TEXT,
    val searchText: String = "",
    val topKText: String = "36",
    val queryImage: QueryImageUiState? = null,
    val isSearching: Boolean = false,
    val searchStatus: String? = null,
    val isLoadingNextPage: Boolean = false,
    val results: PersistentList<SearchItem> = persistentListOf(),
    val totalResults: Int = 0,
    val activeSearchId: String? = null,
    val nextPage: Int = 1,
    val recommendations: RecommendationUiState = RecommendationUiState(),
    val viewerSource: ViewerSource? = null,
    val viewerIndex: Int? = null,
    val transferMessage: String? = null,
    val transferFraction: Float? = null,
    val savedFiles: List<SavedFileRecord> = emptyList(),
)

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
    hasImage = queryImage?.queryImageId != null,
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
