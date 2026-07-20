package com.zvec.lanviewer.ui

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.local.ServerAddress
import com.zvec.lanviewer.data.model.DiscoveredServer
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.SearchMode
import com.zvec.lanviewer.data.model.SearchRequest
import com.zvec.lanviewer.data.model.SearchPageResponse
import com.zvec.lanviewer.data.model.SearchPageResult
import com.zvec.lanviewer.data.network.ApiException
import com.zvec.lanviewer.data.repository.PairingAttempt
import com.zvec.lanviewer.data.repository.ZvecRepository
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.collections.immutable.persistentListOf
import java.io.IOException

class AppViewModel(
    private val appContext: Context,
    private val repository: ZvecRepository,
) : ViewModel() {
    private val _state = MutableStateFlow(AppUiState())
    val state: StateFlow<AppUiState> = _state.asStateFlow()

    private val _events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 4)
    val events: SharedFlow<AppEvent> = _events.asSharedFlow()

    private var pairingJob: Job? = null
    private var uploadJob: Job? = null
    private var pageJob: Job? = null
    private var searchJob: Job? = null
    private var actionJob: Job? = null

    init {
        restoreOrDiscover()
    }

    fun discover() {
        viewModelScope.launch {
            _state.update {
                it.copy(
                    phase = ConnectionPhase.DISCOVERY,
                    isDiscovering = true,
                    errorMessage = null,
                    pairingCode = null,
                )
            }
            try {
                val servers = repository.discover()
                _state.update {
                    it.copy(
                        isDiscovering = false,
                        discoveredServers = servers,
                        errorMessage = if (servers.isEmpty()) "未自动发现电脑，可在下方手工输入 IP。" else null,
                    )
                }
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                _state.update { it.copy(isDiscovering = false, errorMessage = userMessage(error)) }
            }
        }
    }

    fun connect(server: DiscoveredServer) {
        val baseUrl = ServerAddress.fromDiscovery(server)
        if (baseUrl == null) {
            showMessage("发现结果中的地址无效")
            return
        }
        connect(
            SavedConnection(
                baseUrl = baseUrl,
                instanceId = server.instanceId,
                displayName = server.name,
            ),
        )
    }

    fun connectManual(host: String, portText: String) {
        val port = portText.toIntOrNull()
        val baseUrl = port?.let { ServerAddress.fromHostPort(host, it) }
        if (baseUrl == null) {
            showMessage("请输入有效的电脑 IP 和端口（1–65535）")
            return
        }
        connect(SavedConnection(baseUrl, instanceId = null, displayName = host.trim()))
    }

    fun cancelPairing() {
        pairingJob?.cancel()
        pairingJob = null
        _state.update {
            it.copy(
                phase = ConnectionPhase.DISCOVERY,
                pairingCode = null,
                pairingSecondsRemaining = 0,
            )
        }
    }

    fun setSearchMode(mode: SearchMode) {
        _state.update { it.copy(searchMode = mode, errorMessage = null) }
    }

    fun setSearchText(text: String) {
        _state.update { it.copy(searchText = text) }
    }

    fun setTopK(text: String) {
        if (text.all(Char::isDigit) && text.length <= 9) _state.update { it.copy(topKText = text) }
    }

    fun toggleLibrary(libraryId: String) {
        _state.update { current ->
            val updated = current.selectedLibraryIds.toMutableSet().apply {
                if (!add(libraryId)) remove(libraryId)
            }
            current.copy(selectedLibraryIds = updated)
        }
    }

    fun selectQueryImage(uri: Uri) {
        uploadJob?.cancel()
        uploadJob = viewModelScope.launch {
            val resolver = appContext.contentResolver
            val name = resolver.displayName(uri) ?: "query-image"
            val mime = resolver.getType(uri) ?: "application/octet-stream"
            val previousId = _state.value.queryImage?.queryImageId
            _state.update {
                it.copy(
                    queryImage = QueryImageUiState(
                        uri = uri,
                        displayName = name,
                        mimeType = mime,
                        uploading = true,
                    ),
                    errorMessage = null,
                )
            }
            try {
                previousId?.let { runCatching { repository.deleteQueryImage(it) } }
                val uploaded = repository.uploadQueryImage(resolver, uri, name, mime) { progress ->
                    _state.update { current ->
                        current.copy(
                            queryImage = current.queryImage?.copy(
                                bytesUploaded = progress.bytesWritten,
                                totalBytes = progress.contentLength,
                            ),
                        )
                    }
                }
                _state.update {
                    it.copy(
                        queryImage = it.queryImage?.copy(
                            queryImageId = uploaded.queryImageId,
                            uploading = false,
                        ),
                    )
                }
            } catch (error: Throwable) {
                if (error is CancellationException) {
                    _state.update { it.copy(queryImage = null) }
                    return@launch
                }
                _state.update {
                    it.copy(
                        queryImage = it.queryImage?.copy(uploading = false),
                        errorMessage = userMessage(error),
                    )
                }
            }
        }
    }

    fun cancelQueryUpload() {
        uploadJob?.cancel()
        uploadJob = null
        _state.update { it.copy(queryImage = null) }
    }

    fun clearQueryImage() {
        val queryId = _state.value.queryImage?.queryImageId
        uploadJob?.cancel()
        _state.update { it.copy(queryImage = null) }
        if (queryId != null) viewModelScope.launch { runCatching { repository.deleteQueryImage(queryId) } }
    }

    fun search() {
        if (_state.value.isSearching) return
        pageJob?.cancel()
        searchJob = viewModelScope.launch {
            val snapshot = _state.value
            val topK = snapshot.topKText.toIntOrNull()?.takeIf { it > 0 }
            val validationError = when {
                topK == null -> "结果数量必须是正整数"
                snapshot.searchMode in setOf(SearchMode.TEXT, SearchMode.TAG) && snapshot.searchText.isBlank() ->
                    "请输入搜索文字"
                snapshot.searchMode in setOf(SearchMode.IMAGE, SearchMode.COMBINED) &&
                    snapshot.queryImage?.queryImageId == null -> "请先选择并上传查询图片"
                snapshot.searchMode == SearchMode.COMBINED && snapshot.searchText.isBlank() ->
                    "图文联合搜索还需要输入文字"
                else -> null
            }
            if (validationError != null) {
                showMessage(validationError)
                return@launch
            }

            _state.update {
                it.copy(
                    isSearching = true,
                    searchStatus = "正在创建搜索…",
                    errorMessage = null,
                    results = persistentListOf(),
                    totalResults = 0,
                    viewerIndex = null,
                )
            }
            try {
                snapshot.activeSearchId?.let { runCatching { repository.deleteSearch(it) } }
                val created = repository.createSearch(
                    SearchRequest(
                        mode = snapshot.searchMode,
                        text = snapshot.searchText.trim().takeIf(String::isNotBlank),
                        queryImageId = snapshot.queryImage?.queryImageId,
                        libraryIds = snapshot.selectedLibraryIds.toList(),
                        topK = requireNotNull(topK),
                    ),
                )
                _state.update {
                    it.copy(
                        activeSearchId = created.searchId,
                        nextPage = 1,
                        isSearching = true,
                        searchStatus = "等待电脑完成搜索…",
                    )
                }
                loadNextPage()
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                _state.update { it.copy(isSearching = false, searchStatus = null, errorMessage = userMessage(error)) }
            }
        }
    }

    fun cancelSearch() {
        val searchId = _state.value.activeSearchId
        searchJob?.cancel()
        searchJob = null
        pageJob?.cancel()
        pageJob = null
        _state.update {
            it.copy(
                isSearching = false,
                isLoadingNextPage = false,
                searchStatus = null,
                activeSearchId = null,
            )
        }
        if (searchId != null) viewModelScope.launch { runCatching { repository.deleteSearch(searchId) } }
    }

    fun loadNextPage() {
        val snapshot = _state.value
        if (snapshot.isLoadingNextPage || snapshot.activeSearchId == null) return
        if (snapshot.totalResults > 0 && snapshot.results.size >= snapshot.totalResults) return
        pageJob = viewModelScope.launch {
            _state.update { it.copy(isLoadingNextPage = true) }
            try {
                val current = _state.value
                val page = awaitReadyPage(
                    searchId = requireNotNull(current.activeSearchId),
                    page = current.nextPage,
                    pageSize = SEARCH_PAGE_SIZE,
                )
                _state.update {
                    it.copy(
                        isLoadingNextPage = false,
                        isSearching = false,
                        searchStatus = null,
                        results = it.results.addAll(page.items),
                        totalResults = page.total,
                        nextPage = page.page + 1,
                    )
                }
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                _state.update {
                    it.copy(
                        isLoadingNextPage = false,
                        isSearching = false,
                        searchStatus = null,
                        errorMessage = userMessage(error),
                    )
                }
            }
        }
    }

    private suspend fun awaitReadyPage(searchId: String, page: Int, pageSize: Int): SearchPageResponse {
        val deadline = System.currentTimeMillis() + SEARCH_TIMEOUT_MS
        var localBackoffMs = 500L
        while (System.currentTimeMillis() < deadline) {
            when (val result = repository.searchPage(searchId, page, pageSize)) {
                is SearchPageResult.Ready -> return result.page
                is SearchPageResult.Pending -> {
                    _state.update {
                        it.copy(searchStatus = if (result.status == "queued") "搜索正在排队…" else "电脑正在搜索…")
                    }
                    val serverDelayMs = result.retryAfterSeconds * 1_000L
                    delay(maxOf(serverDelayMs, localBackoffMs).coerceAtMost(5_000L))
                    localBackoffMs = (localBackoffMs * 3L / 2L).coerceAtMost(5_000L)
                }
            }
        }
        runCatching { repository.deleteSearch(searchId) }
        throw IOException("搜索等待超时，请重试")
    }

    fun mediaUrl(item: SearchItem): String = repository.mediaUrl(item.mediaId)

    fun openViewer(index: Int) {
        if (index in _state.value.results.indices) _state.update { it.copy(viewerIndex = index) }
    }

    fun setViewerIndex(index: Int) {
        if (index in _state.value.results.indices) {
            _state.update { it.copy(viewerIndex = index) }
            if (index >= _state.value.results.lastIndex - 4) loadNextPage()
        }
    }

    fun closeViewer() {
        _state.update { it.copy(viewerIndex = null, transferMessage = null, transferFraction = null) }
    }

    fun saveCurrent(destination: Uri) {
        val item = currentViewerItem() ?: return
        actionJob?.cancel()
        actionJob = viewModelScope.launch {
            beginTransfer("正在保存原图…")
            try {
                repository.copyOriginalTo(item, destination) { updateTransfer(it.fraction) }
                endTransfer()
                _events.emit(AppEvent.Message("原图已保存"))
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                endTransfer()
                _events.emit(AppEvent.Message(userMessage(error)))
            }
        }
    }

    fun shareCurrent() {
        val item = currentViewerItem() ?: return
        actionJob?.cancel()
        actionJob = viewModelScope.launch {
            beginTransfer("正在准备原图…")
            try {
                val uri = repository.shareUri(item) { updateTransfer(it.fraction) }
                endTransfer()
                _events.emit(AppEvent.Share(uri, item.contentType ?: "image/*"))
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                endTransfer()
                _events.emit(AppEvent.Message(userMessage(error)))
            }
        }
    }

    fun disconnect() {
        val activeSearchId = _state.value.activeSearchId
        val queryImageId = _state.value.queryImage?.queryImageId
        pairingJob?.cancel()
        uploadJob?.cancel()
        pageJob?.cancel()
        searchJob?.cancel()
        actionJob?.cancel()
        viewModelScope.launch {
            activeSearchId?.let { runCatching { repository.deleteSearch(it) } }
            queryImageId?.let { runCatching { repository.deleteQueryImage(it) } }
            repository.disconnect(revokeServerSession = true)
            _state.value = AppUiState()
            discover()
        }
    }

    private fun restoreOrDiscover() {
        val saved = repository.savedConnection()
        if (saved == null || !repository.hasToken()) {
            discover()
            return
        }
        _state.update { it.copy(phase = ConnectionPhase.CONNECTING) }
        viewModelScope.launch {
            try {
                loadReadyState()
            } catch (_: Throwable) {
                _state.update { it.copy(phase = ConnectionPhase.DISCOVERY) }
                discover()
            }
        }
    }

    private fun connect(connection: SavedConnection) {
        pairingJob?.cancel()
        repository.selectConnection(connection)
        _state.update {
            it.copy(
                phase = ConnectionPhase.CONNECTING,
                errorMessage = null,
                serverName = connection.displayName,
            )
        }
        viewModelScope.launch {
            if (repository.hasToken()) {
                try {
                    loadReadyState()
                    return@launch
                } catch (error: Throwable) {
                    if (error !is ApiException || error.httpStatus != 401) {
                        _state.update { it.copy(phase = ConnectionPhase.DISCOVERY, errorMessage = userMessage(error)) }
                        return@launch
                    }
                }
            }
            startPairing(connection.baseUrl)
        }
    }

    private suspend fun startPairing(baseUrl: String) {
        try {
            val attempt = repository.beginPairing(baseUrl)
            _state.update {
                it.copy(
                    phase = ConnectionPhase.PAIRING,
                    pairingCode = formatPairingCode(attempt.comparisonCode),
                    pairingSecondsRemaining = attempt.expiresInSeconds,
                    errorMessage = null,
                )
            }
            pairingJob = viewModelScope.launch {
                try {
                    pollUntilComplete(attempt)
                } catch (error: Throwable) {
                    if (error is CancellationException) throw error
                    _state.update {
                        it.copy(phase = ConnectionPhase.DISCOVERY, errorMessage = userMessage(error))
                    }
                }
            }
        } catch (error: Throwable) {
            if (error is CancellationException) throw error
            _state.update { it.copy(phase = ConnectionPhase.DISCOVERY, errorMessage = userMessage(error)) }
        }
    }

    private suspend fun pollUntilComplete(attempt: PairingAttempt) {
        val expiresAt = System.currentTimeMillis() + attempt.expiresInSeconds * 1000L
        while (System.currentTimeMillis() < expiresAt) {
            delay(POLL_INTERVAL_MS)
            val remaining = ((expiresAt - System.currentTimeMillis()) / 1000L).coerceAtLeast(0L)
            _state.update { it.copy(pairingSecondsRemaining = remaining) }
            when (repository.pollPairing(attempt)) {
                "pending" -> Unit
                "approved" -> {
                    loadReadyState()
                    return
                }
                "rejected" -> {
                    _state.update {
                        it.copy(phase = ConnectionPhase.DISCOVERY, errorMessage = "电脑端拒绝了配对请求")
                    }
                    return
                }
                "expired" -> break
            }
        }
        _state.update { it.copy(phase = ConnectionPhase.DISCOVERY, errorMessage = "配对请求已过期，请重试") }
    }

    private suspend fun loadReadyState() {
        val status = repository.status()
        val libraries = repository.libraries()
        val saved = repository.savedConnection()
        if (saved != null && saved.instanceId != status.instanceId) {
            repository.selectConnection(
                saved.copy(instanceId = status.instanceId, displayName = status.name),
            )
        }
        _state.update {
            it.copy(
                phase = ConnectionPhase.READY,
                serverName = status.name,
                libraries = libraries.libraries,
                errorMessage = null,
                pairingCode = null,
            )
        }
    }

    private fun currentViewerItem(): SearchItem? =
        _state.value.viewerIndex?.let(_state.value.results::getOrNull)

    private fun beginTransfer(message: String) {
        _state.update { it.copy(transferMessage = message, transferFraction = null) }
    }

    private fun updateTransfer(fraction: Float?) {
        _state.update { it.copy(transferFraction = fraction) }
    }

    private fun endTransfer() {
        _state.update { it.copy(transferMessage = null, transferFraction = null) }
    }

    private fun showMessage(message: String) {
        _state.update { it.copy(errorMessage = message) }
        _events.tryEmit(AppEvent.Message(message))
    }

    private fun userMessage(error: Throwable): String = when (error) {
        is ApiException -> error.message
        is IOException -> error.message ?: "网络或文件操作失败"
        else -> "操作失败，请重试"
    }

    private fun formatPairingCode(raw: String): String {
        val digits = raw.filter(Char::isDigit).take(6)
        return if (digits.length == 6) "${digits.take(3)} ${digits.takeLast(3)}" else raw.take(12)
    }

    class Factory(
        private val context: Context,
        private val repository: ZvecRepository,
    ) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T {
            require(modelClass.isAssignableFrom(AppViewModel::class.java))
            return AppViewModel(context.applicationContext, repository) as T
        }
    }

    companion object {
        private const val POLL_INTERVAL_MS = 2_000L
        private const val SEARCH_TIMEOUT_MS = 5 * 60 * 1000L
    }
}

internal const val SEARCH_PAGE_SIZE = 100

private fun ContentResolver.displayName(uri: Uri): String? = runCatching {
    query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
        if (cursor.moveToFirst()) cursor.getString(0) else null
    }
}.getOrNull()
