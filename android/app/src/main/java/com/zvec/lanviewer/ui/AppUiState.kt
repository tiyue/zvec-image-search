package com.zvec.lanviewer.ui

import android.net.Uri
import com.zvec.lanviewer.data.model.DiscoveredServer
import com.zvec.lanviewer.data.model.LibraryDto
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

data class AppUiState(
    val phase: ConnectionPhase = ConnectionPhase.DISCOVERY,
    val isDiscovering: Boolean = false,
    val discoveredServers: List<DiscoveredServer> = emptyList(),
    val errorMessage: String? = null,
    val pairingCode: String? = null,
    val pairingSecondsRemaining: Long = 0L,
    val serverName: String? = null,
    val libraries: List<LibraryDto> = emptyList(),
    val selectedLibraryIds: Set<String> = emptySet(),
    val searchMode: SearchMode = SearchMode.TEXT,
    val searchText: String = "",
    val topKText: String = "1000",
    val queryImage: QueryImageUiState? = null,
    val isSearching: Boolean = false,
    val searchStatus: String? = null,
    val isLoadingNextPage: Boolean = false,
    val results: PersistentList<SearchItem> = persistentListOf(),
    val totalResults: Int = 0,
    val activeSearchId: String? = null,
    val nextPage: Int = 1,
    val viewerIndex: Int? = null,
    val transferMessage: String? = null,
    val transferFraction: Float? = null,
)

sealed interface AppEvent {
    data class Message(val text: String) : AppEvent
    data class Share(val uri: Uri, val mimeType: String) : AppEvent
}
