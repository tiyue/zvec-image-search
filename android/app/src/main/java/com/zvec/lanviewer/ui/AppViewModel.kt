package com.zvec.lanviewer.ui

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.os.SystemClock
import android.provider.OpenableColumns
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import coil.imageLoader
import coil.request.ImageRequest
import coil.request.SuccessResult
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.local.SavedFileRecord
import com.zvec.lanviewer.data.local.SavedFilesStore
import com.zvec.lanviewer.data.local.ServerAddress
import com.zvec.lanviewer.data.model.DiscoveredServer
import com.zvec.lanviewer.data.model.OriginalMediaItem
import com.zvec.lanviewer.data.model.RecommendationAction
import com.zvec.lanviewer.data.model.RecommendationActionRequest
import com.zvec.lanviewer.data.model.RecommendationActionResponse
import com.zvec.lanviewer.data.model.RecommendationItem
import com.zvec.lanviewer.data.model.RecommendationPreference
import com.zvec.lanviewer.data.model.RecommendationRequest
import com.zvec.lanviewer.data.model.RecommendationShownRequest
import com.zvec.lanviewer.data.model.RecommendationsResponse
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.SearchMode
import com.zvec.lanviewer.data.model.SearchRequest
import com.zvec.lanviewer.data.model.SearchPageResponse
import com.zvec.lanviewer.data.model.SearchPageResult
import com.zvec.lanviewer.data.network.ApiException
import com.zvec.lanviewer.data.repository.PairingAttempt
import com.zvec.lanviewer.data.repository.PairingTokenMissingException
import com.zvec.lanviewer.data.repository.ZvecRepository
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeout
import kotlinx.collections.immutable.persistentListOf
import kotlinx.collections.immutable.toPersistentList
import java.io.IOException
import java.util.UUID

private const val RECOMMENDATION_SHOWN_RETRY_INITIAL_DELAY_MILLIS = 1_000L
private const val RECOMMENDATION_SHOWN_RETRY_MAX_DELAY_MILLIS = 30_000L

internal enum class RecommendationShownJobDecision {
    START,
    KEEP_ACTIVE,
}

internal fun recommendationShownJobDecision(
    requestedBatchId: String,
    activeBatchIds: Set<String>,
): RecommendationShownJobDecision = when {
    requestedBatchId in activeBatchIds -> RecommendationShownJobDecision.KEEP_ACTIVE
    else -> RecommendationShownJobDecision.START
}

internal fun recommendationReactions(
    items: List<RecommendationItem>,
): Map<String, RecommendationAction> = items.mapNotNull { item ->
    val action = when (item.preference) {
        RecommendationPreference.LIKE -> RecommendationAction.LIKE
        RecommendationPreference.DISLIKE -> RecommendationAction.DISLIKE
        null -> null
    }
    action?.let { item.itemId to it }
}.toMap()

internal fun canSubmitRecommendationReaction(
    reactions: Map<String, RecommendationAction>,
    pendingItemIds: Set<String>,
    itemId: String,
    action: RecommendationAction,
): Boolean = action in setOf(RecommendationAction.LIKE, RecommendationAction.DISLIKE) &&
    itemId !in pendingItemIds && reactions[itemId] != action

internal fun recommendationActionEventIdsAfterSuccess(
    actionEventIds: Map<String, String>,
    itemId: String,
    action: RecommendationAction,
): Map<String, String> {
    if (action !in setOf(RecommendationAction.LIKE, RecommendationAction.DISLIKE)) {
        return actionEventIds - "$itemId:${action.name}"
    }
    return actionEventIds -
        "$itemId:${RecommendationAction.LIKE.name}" -
        "$itemId:${RecommendationAction.DISLIKE.name}"
}

internal fun recommendationReactionsAfterActionSuccess(
    reactions: Map<String, RecommendationAction>,
    itemId: String,
    requestedAction: RecommendationAction,
    response: RecommendationActionResponse,
): Map<String, RecommendationAction> {
    if (requestedAction !in setOf(RecommendationAction.LIKE, RecommendationAction.DISLIKE)) {
        return reactions
    }
    val confirmedAction = if (response.preferenceProvided) {
        when (response.preference) {
            RecommendationPreference.LIKE -> RecommendationAction.LIKE
            RecommendationPreference.DISLIKE -> RecommendationAction.DISLIKE
            null -> null
        }
    } else {
        requestedAction
    }
    return if (confirmedAction == null) {
        reactions - itemId
    } else {
        reactions + (itemId to confirmedAction)
    }
}

internal fun recommendationPreferenceChangedAfterAction(
    previous: RecommendationAction?,
    requestedAction: RecommendationAction,
    response: RecommendationActionResponse,
): Boolean {
    if (requestedAction !in setOf(RecommendationAction.LIKE, RecommendationAction.DISLIKE)) return false
    val confirmedAction = if (response.preferenceProvided) {
        when (response.preference) {
            RecommendationPreference.LIKE -> RecommendationAction.LIKE
            RecommendationPreference.DISLIKE -> RecommendationAction.DISLIKE
            null -> null
        }
    } else {
        requestedAction
    }
    return confirmedAction != previous
}

internal suspend fun retryRecommendationShown(
    isCurrentBatch: () -> Boolean,
    submit: suspend () -> Unit,
): Boolean {
    var retryDelayMillis = RECOMMENDATION_SHOWN_RETRY_INITIAL_DELAY_MILLIS
    while (isCurrentBatch()) {
        try {
            submit()
            return true
        } catch (error: Throwable) {
            if (error is CancellationException) throw error
            if (!isRetryableRecommendationShownError(error)) return false
        }
        if (!isCurrentBatch()) return false
        delay(retryDelayMillis)
        retryDelayMillis = (retryDelayMillis * 2).coerceAtMost(
            RECOMMENDATION_SHOWN_RETRY_MAX_DELAY_MILLIS,
        )
    }
    return false
}

private fun isRetryableRecommendationShownError(error: Throwable): Boolean = when (error) {
    is ApiException -> error.httpStatus == 408 || error.httpStatus == 429 || error.httpStatus >= 500
    is IOException -> true
    else -> false
}

class AppViewModel(
    private val appContext: Context,
    private val repository: ZvecRepository,
    private val savedFilesStore: SavedFilesStore,
) : ViewModel() {
    private val _state = MutableStateFlow(AppUiState())
    val state: StateFlow<AppUiState> = _state.asStateFlow()

    private val _events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 4)
    val events: SharedFlow<AppEvent> = _events.asSharedFlow()

    private val connectionFlight = KeyedSingleFlight(viewModelScope)
    private var discoveryJob: Job? = null
    private val discoveryGeneration = OperationGeneration()
    private var uploadJob: Job? = null
    private var pageJob: Job? = null
    private var searchJob: Job? = null
    private var actionJob: Job? = null
    private var recommendationJob: Job? = null
    private val recommendationShownJobs = mutableMapOf<String, Job>()
    private var recommendationActionJob: Job? = null
    private var recommendationTailJob: Job? = null
    private var recommendationPreparationMonitorJob: Job? = null
    private var pendingRecommendationPreload: RecommendationPreloadTask<RecommendationsResponse>? = null
    private var currentRecommendationPreload: RecommendationPreloadTask<RecommendationsResponse>? = null
    private var preparedRecommendation: RecommendationPreloadTask<RecommendationsResponse>? = null
    private var recommendationGeneration = 0L
    private var recommendationsVisible = false
    private var settledRecommendationBatchId: String? = null

    init {
        _state.update { it.copy(savedFiles = savedFilesStore.list()) }
        restoreOrDiscover()
    }

    fun discover() {
        connectionFlight.cancel()
        discover(preservedError = null)
    }

    private fun discover(preservedError: String?) {
        cancelDiscovery()
        val generation = discoveryGeneration.begin()
        val next = viewModelScope.launch(start = CoroutineStart.LAZY) {
            _state.update {
                it.copy(
                    phase = ConnectionPhase.DISCOVERY,
                    isDiscovering = true,
                    errorMessage = preservedError,
                    pairingCode = null,
                    connectionDetail = null,
                )
            }
            try {
                val servers = repository.discover()
                if (!discoveryGeneration.isCurrent(generation)) return@launch
                _state.update {
                    it.copy(
                        isDiscovering = false,
                        discoveredServers = servers,
                        errorMessage = preservedError
                            ?: if (servers.isEmpty()) "未自动发现电脑，可在下方手工输入 IP。" else null,
                    )
                }
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                if (discoveryGeneration.isCurrent(generation)) {
                    _state.update { it.copy(isDiscovering = false, errorMessage = userMessage(error)) }
                }
            }
        }
        discoveryJob = next
        next.invokeOnCompletion {
            if (discoveryJob === next) discoveryJob = null
        }
        next.start()
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
        connectionFlight.cancel()
        _state.update {
            it.copy(
                phase = ConnectionPhase.DISCOVERY,
                pairingCode = null,
                pairingSecondsRemaining = 0,
                connectionDetail = null,
            )
        }
    }

    fun setSearchMode(mode: SearchMode) {
        val visibleMode = if (mode == SearchMode.TAG) SearchMode.TAG else SearchMode.TEXT
        if (visibleMode == SearchMode.TAG) clearQueryImage()
        _state.update { it.copy(searchMode = visibleMode, errorMessage = null) }
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
            val requestMode = snapshot.resolvedSearchMode()
            val topK = snapshot.topKText.toIntOrNull()?.takeIf { it > 0 }
            val validationError = when {
                topK == null -> "结果数量必须是正整数"
                requestMode in setOf(SearchMode.TEXT, SearchMode.TAG) && snapshot.searchText.isBlank() ->
                    "请输入搜索文字"
                requestMode in setOf(SearchMode.IMAGE, SearchMode.COMBINED) &&
                    snapshot.queryImage?.queryImageId == null -> "请先选择并上传查询图片"
                requestMode == SearchMode.COMBINED && snapshot.searchText.isBlank() ->
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
                        mode = requestMode,
                        text = snapshot.searchText.trim().takeIf(String::isNotBlank),
                        queryImageId = snapshot.queryImage?.queryImageId.takeIf { requestMode != SearchMode.TAG },
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

    fun mediaUrl(item: OriginalMediaItem): String = repository.mediaUrl(item.mediaId)

    fun openViewer(index: Int) {
        if (index in _state.value.results.indices) {
            _state.update { it.copy(viewerSource = ViewerSource.SEARCH, viewerIndex = index) }
        }
    }

    fun setViewerIndex(index: Int) {
        if (index in _state.value.viewerItems().indices) {
            _state.update { it.copy(viewerIndex = index) }
            if (_state.value.viewerSource == ViewerSource.SEARCH && index >= _state.value.results.lastIndex - 4) {
                loadNextPage()
            }
        }
    }

    fun closeViewer() {
        _state.update {
            it.copy(
                viewerSource = null,
                viewerIndex = null,
                transferMessage = null,
                transferFraction = null,
            )
        }
    }

    fun loadRecommendations() {
        if (_state.value.recommendations.isLoading) return
        val prefetched = preparedRecommendation
        preparedRecommendation = null
        recommendationPreparationMonitorJob?.cancel()
        recommendationPreparationMonitorJob = null
        cancelCurrentRecommendationTail()
        val generation = recommendationGeneration
        recommendationJob?.cancel()
        val next = viewModelScope.launch(start = CoroutineStart.LAZY) {
            _state.update { current ->
                current.copy(recommendations = current.recommendations.copy(isLoading = true, errorMessage = null))
            }
            var preload = prefetched ?: startRecommendationPreload(generation)
            pendingRecommendationPreload = preload
            try {
                var response = try {
                    preload.awaitCritical()
                } catch (error: Throwable) {
                    if (error is CancellationException) throw error
                    if (prefetched == null || !isRecommendationGenerationCurrent(generation)) throw error
                    preload.cancel()
                    preload = startRecommendationPreload(generation)
                    pendingRecommendationPreload = preload
                    preload.awaitCritical()
                }
                if (!isRecommendationGenerationCurrent(generation)) {
                    preload.cancel()
                    return@launch
                }
                if (response.requestId != preload.requestId) throw IOException("推荐响应与请求不一致")
                pendingRecommendationPreload = null
                currentRecommendationPreload = preload
                settledRecommendationBatchId = null
                _state.update {
                    it.copy(
                        recommendations = RecommendationUiState(
                            batchId = response.batchId,
                            preloadedBatchId = response.batchId,
                            items = response.items.toPersistentList(),
                            partial = response.partial,
                            partialReason = response.partialReason,
                            reactions = recommendationReactions(response.items),
                        ),
                    )
                }
                observeCurrentRecommendationPreload(preload, response.batchId)
                onRecommendationsVisible()
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                _state.update { current ->
                    current.copy(
                        recommendations = current.recommendations.copy(
                            isLoading = false,
                            errorMessage = userMessage(error),
                        ),
                    )
                }
            } finally {
                if (pendingRecommendationPreload === preload) pendingRecommendationPreload = null
            }
        }
        recommendationJob = next
        next.invokeOnCompletion {
            if (recommendationJob === next) recommendationJob = null
        }
        next.start()
    }

    fun onRecommendationsVisibilityChanged(visible: Boolean) {
        if (recommendationsVisible == visible) return
        recommendationsVisible = visible
        if (!visible) {
            cancelRecommendationDelivery()
            return
        }
        recommendationGeneration += 1
        val recommendation = _state.value.recommendations
        when {
            recommendation.batchId == null && !recommendation.isLoading -> loadRecommendations()
            recommendation.batchId != null -> {
                onRecommendationsVisible()
                if (settledRecommendationBatchId == recommendation.batchId) {
                    maybePrepareNextRecommendations()
                } else if (currentRecommendationPreload == null) {
                    resumeCurrentRecommendationPreload()
                }
            }
        }
    }

    fun onRecommendationsVisible() {
        if (!recommendationsVisible) return
        val recommendation = _state.value.recommendations
        val batchId = recommendation.batchId ?: return
        if (recommendation.preloadedBatchId != batchId || recommendation.items.isEmpty() ||
            recommendation.shownBatchId == batchId
        ) return
        when (
            recommendationShownJobDecision(
                requestedBatchId = batchId,
                activeBatchIds = recommendationShownJobs
                    .filterValues { it.isActive }
                    .keys,
            )
        ) {
            RecommendationShownJobDecision.KEEP_ACTIVE -> return
            RecommendationShownJobDecision.START -> Unit
        }
        val eventId = recommendation.shownEventId ?: UUID.randomUUID().toString().also { generatedId ->
            _state.update { current ->
                if (current.recommendations.batchId == batchId) {
                    current.copy(recommendations = current.recommendations.copy(shownEventId = generatedId))
                } else current
            }
        }
        val next = viewModelScope.launch(start = CoroutineStart.LAZY) {
            val recorded = retryRecommendationShown(
                isCurrentBatch = {
                    recommendationsVisible
                },
                submit = {
                    repository.markRecommendationsShown(batchId, RecommendationShownRequest(eventId))
                },
            )
            if (recorded) {
                _state.update { current ->
                    if (current.recommendations.batchId == batchId) {
                        current.copy(
                            recommendations = current.recommendations.copy(
                                shownBatchId = batchId,
                                shownEventId = null,
                            ),
                        )
                    } else current
                }
                maybePrepareNextRecommendations()
            }
        }
        recommendationShownJobs[batchId] = next
        next.invokeOnCompletion {
            if (recommendationShownJobs[batchId] === next) {
                recommendationShownJobs.remove(batchId)
            }
        }
        next.start()
    }

    fun openRecommendation(index: Int) {
        val item = _state.value.recommendations.items.getOrNull(index) ?: return
        _state.update { it.copy(viewerSource = ViewerSource.RECOMMENDATIONS, viewerIndex = index) }
        recordRecommendationAction(item, RecommendationAction.OPEN)
    }

    fun reactToRecommendation(itemId: String, action: RecommendationAction) {
        val recommendations = _state.value.recommendations
        if (
            !canSubmitRecommendationReaction(
                reactions = recommendations.reactions,
                pendingItemIds = recommendations.pendingReactionItemIds,
                itemId = itemId,
                action = action,
            )
        ) return
        val item = recommendations.items.firstOrNull { it.itemId == itemId } ?: return
        recordRecommendationAction(item, action)
    }

    private fun recordRecommendationAction(
        item: RecommendationItem,
        action: RecommendationAction,
        metadata: Map<String, String>? = null,
    ) {
        val batchId = _state.value.recommendations.batchId ?: return
        val actionKey = "${item.itemId}:${action.name}"
        val eventId = _state.value.recommendations.actionEventIds[actionKey] ?: UUID.randomUUID().toString()
        val previousReaction = _state.value.recommendations.reactions[item.itemId]
        if (actionKey in _state.value.recommendations.pendingActionKeys) return
        _state.update { current ->
            if (current.recommendations.batchId != batchId) return@update current
            current.copy(
                recommendations = current.recommendations.copy(
                    actionEventIds = current.recommendations.actionEventIds + (actionKey to eventId),
                    pendingActionKeys = current.recommendations.pendingActionKeys + actionKey,
                    pendingReactionItemIds = if (action in setOf(RecommendationAction.LIKE, RecommendationAction.DISLIKE)) {
                        current.recommendations.pendingReactionItemIds + item.itemId
                    } else current.recommendations.pendingReactionItemIds,
                ),
            )
        }
        recommendationActionJob = viewModelScope.launch {
            try {
                val response = repository.recordRecommendationAction(
                    batchId,
                    RecommendationActionRequest(eventId, item.itemId, action, metadata),
                )
                var preferenceChanged = false
                _state.update { current ->
                    if (current.recommendations.batchId != batchId) return@update current
                    val updatedReactions = recommendationReactionsAfterActionSuccess(
                        current.recommendations.reactions,
                        item.itemId,
                        action,
                        response,
                    )
                    preferenceChanged = recommendationPreferenceChangedAfterAction(
                        previous = previousReaction,
                        requestedAction = action,
                        response = response,
                    )
                    current.copy(
                        recommendations = current.recommendations.copy(
                            reactions = updatedReactions,
                            actionEventIds = recommendationActionEventIdsAfterSuccess(
                                current.recommendations.actionEventIds,
                                item.itemId,
                                action,
                            ),
                            pendingActionKeys = current.recommendations.pendingActionKeys - actionKey,
                            pendingReactionItemIds = if (action in setOf(RecommendationAction.LIKE, RecommendationAction.DISLIKE)) {
                                current.recommendations.pendingReactionItemIds - item.itemId
                            } else current.recommendations.pendingReactionItemIds,
                        ),
                    )
                }
                if (preferenceChanged) invalidatePreparedRecommendations()
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                _state.update { current ->
                    if (current.recommendations.batchId != batchId) return@update current
                    current.copy(
                        recommendations = current.recommendations.copy(
                            pendingActionKeys = current.recommendations.pendingActionKeys - actionKey,
                            pendingReactionItemIds = if (action in setOf(RecommendationAction.LIKE, RecommendationAction.DISLIKE)) {
                                current.recommendations.pendingReactionItemIds - item.itemId
                            } else current.recommendations.pendingReactionItemIds,
                        ),
                    )
                }
            }
        }
    }

    private fun startRecommendationPreload(generation: Long): RecommendationPreloadTask<RecommendationsResponse> {
        val requestId = UUID.randomUUID().toString()
        return viewModelScope.startRecommendationPreload(
            requestId = requestId,
            generation = generation,
            criticalCount = RECOMMENDATION_CRITICAL_THUMBNAIL_COUNT,
            create = { generatedRequestId ->
                repository.recommendations(RecommendationRequest(generatedRequestId)).also { response ->
                    if (response.requestId != generatedRequestId) {
                        throw IOException("推荐响应与请求不一致")
                    }
                }
            },
            thumbnailUrls = { response -> response.items.map(RecommendationItem::thumbnailUrl) },
            preload = ::preloadRecommendationThumbnail,
        )
    }

    private suspend fun preloadRecommendationThumbnail(url: String): Boolean = try {
        appContext.imageLoader.execute(
            ImageRequest.Builder(appContext).data(url).size(640).build(),
        ) is SuccessResult
    } catch (error: CancellationException) {
        throw error
    } catch (_: Throwable) {
        false
    }

    private fun observeCurrentRecommendationPreload(
        preload: RecommendationPreloadTask<RecommendationsResponse>,
        batchId: String,
    ) {
        recommendationTailJob?.cancel()
        val next = viewModelScope.launch {
            val outcome = preload.awaitSettled()
            if (currentRecommendationPreload !== preload || _state.value.recommendations.batchId != batchId) {
                return@launch
            }
            currentRecommendationPreload = null
            settledRecommendationBatchId = batchId
            if (outcome.failedCount > 0) {
                _state.update { current ->
                    if (current.recommendations.batchId == batchId) {
                        current.copy(
                            recommendations = current.recommendations.copy(
                                errorMessage = "部分缩略图加载失败",
                            ),
                        )
                    } else current
                }
            }
            maybePrepareNextRecommendations()
        }
        recommendationTailJob = next
        next.invokeOnCompletion {
            if (recommendationTailJob === next) recommendationTailJob = null
        }
    }

    private fun resumeCurrentRecommendationPreload() {
        val recommendation = _state.value.recommendations
        val batchId = recommendation.batchId ?: return
        if (!recommendationsVisible || recommendation.items.isEmpty() || recommendationTailJob?.isActive == true) return
        val items = recommendation.items.toList()
        val next = viewModelScope.launch {
            val results = coroutineScope {
                items.map { item -> async { preloadRecommendationThumbnail(item.thumbnailUrl) } }.awaitAll()
            }
            if (!recommendationsVisible || _state.value.recommendations.batchId != batchId) return@launch
            settledRecommendationBatchId = batchId
            if (results.any { !it }) {
                _state.update { current ->
                    if (current.recommendations.batchId == batchId) {
                        current.copy(
                            recommendations = current.recommendations.copy(
                                errorMessage = "部分缩略图加载失败",
                            ),
                        )
                    } else current
                }
            }
            maybePrepareNextRecommendations()
        }
        recommendationTailJob = next
        next.invokeOnCompletion {
            if (recommendationTailJob === next) recommendationTailJob = null
        }
    }

    private fun maybePrepareNextRecommendations() {
        val recommendation = _state.value.recommendations
        val batchId = recommendation.batchId ?: return
        if (!recommendationsVisible || recommendation.items.isEmpty() || recommendation.isLoading ||
            recommendation.shownBatchId != batchId || settledRecommendationBatchId != batchId ||
            preparedRecommendation != null || pendingRecommendationPreload != null ||
            currentRecommendationPreload != null || recommendationJob?.isActive == true
        ) return
        val preload = startRecommendationPreload(recommendationGeneration)
        preparedRecommendation = preload
        val monitor = viewModelScope.launch {
            try {
                val outcome = preload.awaitSettled()
                if (outcome.failedCount > 0 && preparedRecommendation === preload) {
                    preparedRecommendation = null
                }
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                if (preparedRecommendation === preload) preparedRecommendation = null
            }
        }
        recommendationPreparationMonitorJob = monitor
        monitor.invokeOnCompletion {
            if (recommendationPreparationMonitorJob === monitor) recommendationPreparationMonitorJob = null
        }
    }

    private fun invalidatePreparedRecommendations() {
        recommendationGeneration += 1
        preparedRecommendation?.cancel()
        preparedRecommendation = null
        recommendationPreparationMonitorJob?.cancel()
        recommendationPreparationMonitorJob = null
        pendingRecommendationPreload?.cancel()
        pendingRecommendationPreload = null
        recommendationJob?.cancel()
        recommendationJob = null
        _state.update { current ->
            current.copy(recommendations = current.recommendations.copy(isLoading = false))
        }
        maybePrepareNextRecommendations()
    }

    private fun cancelCurrentRecommendationTail() {
        currentRecommendationPreload?.cancel()
        currentRecommendationPreload = null
        recommendationTailJob?.cancel()
        recommendationTailJob = null
    }

    private fun cancelRecommendationDelivery() {
        recommendationGeneration += 1
        preparedRecommendation?.cancel()
        preparedRecommendation = null
        recommendationPreparationMonitorJob?.cancel()
        recommendationPreparationMonitorJob = null
        pendingRecommendationPreload?.cancel()
        pendingRecommendationPreload = null
        recommendationJob?.cancel()
        recommendationJob = null
        cancelCurrentRecommendationTail()
        recommendationShownJobs.values.forEach(Job::cancel)
        recommendationShownJobs.clear()
        _state.update { current ->
            current.copy(recommendations = current.recommendations.copy(isLoading = false))
        }
    }

    private fun isRecommendationGenerationCurrent(generation: Long): Boolean =
        recommendationsVisible && recommendationGeneration == generation

    fun saveCurrent(destination: Uri) {
        val item = currentViewerItem() ?: return
        actionJob?.cancel()
        actionJob = viewModelScope.launch {
            beginTransfer("正在保存原图…")
            try {
                repository.copyOriginalTo(item, destination) { updateTransfer(it.fraction) }
                endTransfer()
                savedFilesStore.add(
                    SavedFileRecord(
                        name = item.name.ifBlank { "yaolens-original" },
                        uri = destination.toString(),
                        savedAtMillis = System.currentTimeMillis(),
                        sizeBytes = item.sizeBytes,
                    ),
                )
                _state.update { it.copy(savedFiles = savedFilesStore.list()) }
                (item as? RecommendationItem)?.let { recommendation ->
                    recordRecommendationAction(
                        recommendation,
                        RecommendationAction.EXPORT,
                        metadata = mapOf("channel" to "save"),
                    )
                }
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
        cancelDiscovery()
        connectionFlight.cancel()
        uploadJob?.cancel()
        pageJob?.cancel()
        searchJob?.cancel()
        actionJob?.cancel()
        recommendationsVisible = false
        cancelRecommendationDelivery()
        recommendationActionJob?.cancel()
        viewModelScope.launch {
            activeSearchId?.let { runCatching { repository.deleteSearch(it) } }
            queryImageId?.let { runCatching { repository.deleteQueryImage(it) } }
            repository.disconnect(revokeServerSession = true)
            _state.value = AppUiState()
            discover()
        }
    }

    private fun restoreOrDiscover() {
        cancelDiscovery()
        val saved = repository.savedConnection()
        if (saved == null || !repository.hasToken()) {
            discover()
            return
        }
        _state.update { it.copy(phase = ConnectionPhase.CONNECTING) }
        var rediscoveryMessage: String? = null
        connectionFlight.launch(
            key = saved.baseUrl,
            onComplete = {
                // Completion clears the single-flight slot before discovery starts, so a
                // discovery result can never race a stale restore job.
                rediscoveryMessage?.let { message -> discover(preservedError = message) }
            },
        ) {
            try {
                loadReadyStateWithRetry()
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                rediscoveryMessage = pairingErrorMessage(PairingStage.RESTORE, error)
                failPairing(requireNotNull(rediscoveryMessage))
            }
        }
    }

    private fun connect(connection: SavedConnection) {
        cancelDiscovery()
        connectionFlight.launch(connection.baseUrl) {
            repository.selectConnection(connection)
            _state.update {
                it.copy(
                    phase = ConnectionPhase.CONNECTING,
                    isDiscovering = false,
                    errorMessage = null,
                    serverName = connection.displayName,
                    pairingCode = null,
                    pairingSecondsRemaining = 0,
                    connectionDetail = null,
                )
            }
            if (repository.hasToken()) {
                try {
                    loadReadyStateWithRetry()
                    return@launch
                } catch (error: Throwable) {
                    if (error is CancellationException) throw error
                    if (error !is ApiException || error.httpStatus != 401) {
                        failPairing(PairingStage.RESTORE, error)
                        return@launch
                    }
                }
            }
            startPairing(connection.baseUrl)
        }
    }

    private suspend fun startPairing(baseUrl: String) {
        val attempt = try {
            repository.beginPairing(baseUrl)
        } catch (error: Throwable) {
            if (error is CancellationException) throw error
            failPairing(PairingStage.CREATE, error)
            return
        }
        _state.update {
            it.copy(
                phase = ConnectionPhase.PAIRING,
                pairingCode = formatPairingCode(attempt.comparisonCode),
                pairingSecondsRemaining = normalizedPairingDurationSeconds(attempt.expiresInSeconds),
                connectionDetail = null,
                errorMessage = null,
            )
        }

        val completion = try {
            pollUntilComplete(attempt)
        } catch (error: Throwable) {
            if (error is CancellationException) throw error
            failPairing(PairingStage.POLL, error)
            return
        }
        when (completion) {
            PairingCompletion.APPROVED -> try {
                _state.update(AppUiState::afterPairingApproved)
                loadReadyStateWithRetry()
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                failPairing(PairingStage.COMPLETE, error)
            }
            PairingCompletion.REJECTED -> failPairing("电脑端拒绝了配对请求")
            PairingCompletion.EXPIRED -> failPairing("配对请求已过期，请重试")
        }
    }

    private suspend fun pollUntilComplete(attempt: PairingAttempt): PairingCompletion {
        // Never compare the desktop's wall-clock expires_at with the phone clock: skew can
        // otherwise make a fresh request expire immediately. elapsedRealtime is monotonic.
        val expiresAt = pairingDeadlineElapsedMillis(
            nowElapsedMillis = SystemClock.elapsedRealtime(),
            expiresInSeconds = attempt.expiresInSeconds,
        )
        var lastTransportError: IOException? = null
        val recoveryPolicy = PairingPollRecoveryPolicy()
        while (SystemClock.elapsedRealtime() < expiresAt) {
            val beforeDelay = SystemClock.elapsedRealtime()
            val remainingBeforeDelay = expiresAt - beforeDelay
            if (remainingBeforeDelay <= 0L) break
            delay(minOf(POLL_INTERVAL_MS, remainingBeforeDelay))
            val remainingMillis = expiresAt - SystemClock.elapsedRealtime()
            if (remainingMillis <= 0L) break
            _state.update { it.copy(pairingSecondsRemaining = remainingMillis / 1_000L) }
            val status = try {
                withTimeout(remainingMillis) {
                    repository.pollPairing(attempt).also {
                        lastTransportError = null
                        recoveryPolicy.onSuccessfulStatus()
                    }
                }
            } catch (_: TimeoutCancellationException) {
                break
            } catch (error: ApiException) {
                throw error
            } catch (error: PairingTokenMissingException) {
                if (!recoveryPolicy.shouldRetryApprovedWithoutToken()) throw error
                continue
            } catch (error: IOException) {
                // A momentary Wi-Fi handoff must not discard an already-approved request.
                lastTransportError = error
                continue
            }
            when (status) {
                "pending" -> Unit
                "approved" -> return PairingCompletion.APPROVED
                "rejected" -> return PairingCompletion.REJECTED
                "expired" -> return PairingCompletion.EXPIRED
            }
        }
        lastTransportError?.let { throw it }
        return PairingCompletion.EXPIRED
    }

    private suspend fun loadReadyStateWithRetry() {
        try {
            loadReadyState()
        } catch (error: ApiException) {
            throw error
        } catch (_: IOException) {
            // The token has already been persisted after approval. One transport retry lets
            // a brief Wi-Fi transition recover without forcing the user to approve again.
            delay(CONNECTION_RETRY_DELAY_MS)
            loadReadyState()
        }
    }

    private fun failPairing(stage: PairingStage, error: Throwable) {
        failPairing(pairingErrorMessage(stage, error))
    }

    private fun failPairing(message: String) {
        _state.update {
            it.copy(
                phase = ConnectionPhase.DISCOVERY,
                pairingCode = null,
                pairingSecondsRemaining = 0,
                connectionDetail = null,
                errorMessage = message,
            )
        }
    }

    private fun cancelDiscovery() {
        discoveryGeneration.invalidate()
        discoveryJob?.cancel()
        discoveryJob = null
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
                connectionDetail = null,
            )
        }
    }

    private fun currentViewerItem(): OriginalMediaItem? = _state.value.currentViewerItem()

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

    override fun onCleared() {
        recommendationsVisible = false
        cancelRecommendationDelivery()
        recommendationActionJob?.cancel()
        super.onCleared()
    }

    class Factory(
        private val context: Context,
        private val repository: ZvecRepository,
        private val savedFilesStore: SavedFilesStore,
    ) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T {
            require(modelClass.isAssignableFrom(AppViewModel::class.java))
            return AppViewModel(context.applicationContext, repository, savedFilesStore) as T
        }
    }

    companion object {
        private const val POLL_INTERVAL_MS = 2_000L
        private const val CONNECTION_RETRY_DELAY_MS = 350L
        private const val SEARCH_TIMEOUT_MS = 5 * 60 * 1000L
        private const val RECOMMENDATION_CRITICAL_THUMBNAIL_COUNT = 4
    }
}

private enum class PairingCompletion {
    APPROVED,
    REJECTED,
    EXPIRED,
}

internal fun pairingDeadlineElapsedMillis(nowElapsedMillis: Long, expiresInSeconds: Long): Long {
    return nowElapsedMillis + normalizedPairingDurationSeconds(expiresInSeconds) * 1_000L
}

internal fun normalizedPairingDurationSeconds(expiresInSeconds: Long): Long =
    expiresInSeconds.coerceIn(0L, 10L * 60L)

internal const val SEARCH_PAGE_SIZE = 100

private fun ContentResolver.displayName(uri: Uri): String? = runCatching {
    query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
        if (cursor.moveToFirst()) cursor.getString(0) else null
    }
}.getOrNull()
