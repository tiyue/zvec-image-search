package com.zvec.lanviewer.wear.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.zvec.lanviewer.wear.data.ApiException
import com.zvec.lanviewer.wear.data.PairingStatus
import com.zvec.lanviewer.wear.data.RecommendationItem
import com.zvec.lanviewer.wear.data.SavedConnection
import com.zvec.lanviewer.wear.data.ServerAddress
import com.zvec.lanviewer.wear.data.WatchRepository
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.io.IOException
import java.net.SocketTimeoutException
import java.util.UUID

enum class WatchPage {
    CONNECTION,
    PAIRING,
    RECOMMENDATIONS,
    ORIGINAL,
}

data class RecommendationUiState(
    val batchId: String? = null,
    val items: List<RecommendationItem> = emptyList(),
    val isLoading: Boolean = false,
    val errorMessage: String? = null,
)

data class WatchUiState(
    val page: WatchPage,
    val host: String,
    val port: String,
    val isConnecting: Boolean = false,
    val connectionError: String? = null,
    val comparisonCode: String? = null,
    val pairingMessage: String? = null,
    val canReturnFromSettings: Boolean = false,
    val recommendations: RecommendationUiState = RecommendationUiState(),
    val selectedItem: RecommendationItem? = null,
)

class WatchViewModel(
    private val repository: WatchRepository,
    private val thumbnailLoader: ThumbnailLoader,
    private val originalLoader: OriginalLoader,
) : ViewModel() {
    private val savedAddress = repository.savedConnection()
        ?.let { ServerAddress.hostPort(it.baseUrl) }
    private val startsConnected = savedAddress != null && repository.hasToken()
    private val _state = MutableStateFlow(
        WatchUiState(
            page = if (startsConnected) WatchPage.RECOMMENDATIONS else WatchPage.CONNECTION,
            host = savedAddress?.host ?: ServerAddress.DEFAULT_HOST,
            port = (savedAddress?.port ?: ServerAddress.DEFAULT_PORT).toString(),
        ),
    )
    val state: StateFlow<WatchUiState> = _state.asStateFlow()

    private var generation = 0L
    private var connectionJob: Job? = null
    private var recommendationJob: Job? = null
    private var currentPreload: RecommendationPreloadTask? = null
    private var currentMonitor: Job? = null
    private var preparedPreload: RecommendationPreloadTask? = null
    private var preparedMonitor: Job? = null
    private var shownJob: Job? = null
    private var originalPreloadJob: Job? = null
    private var settledBatchId: String? = null
    private var shownBatchId: String? = null

    init {
        if (startsConnected) loadRecommendations()
    }

    fun updateHost(value: String) {
        _state.update { it.copy(host = value, connectionError = null) }
    }

    fun updatePort(value: String) {
        if (value.all(Char::isDigit) && value.length <= 5) {
            _state.update { it.copy(port = value, connectionError = null) }
        }
    }

    fun connect() {
        if (connectionJob?.isActive == true) return
        val current = _state.value
        val port = current.port.toIntOrNull()
        val baseUrl = port?.let { ServerAddress.fromHostPort(current.host, it) }
        if (baseUrl == null) {
            _state.update { it.copy(connectionError = "请输入正确的 IPv4 和端口") }
            return
        }
        connectionJob = viewModelScope.launch {
            _state.update {
                it.copy(isConnecting = true, connectionError = null, pairingMessage = null)
            }
            try {
                val sameConnection = repository.savedConnection()?.baseUrl == baseUrl
                repository.selectConnection(SavedConnection(baseUrl))
                if (sameConnection && repository.hasToken()) {
                    _state.update {
                        it.copy(page = WatchPage.RECOMMENDATIONS, isConnecting = false)
                    }
                    loadRecommendations()
                    return@launch
                }
                val attempt = repository.beginPairing(baseUrl)
                _state.update {
                    it.copy(
                        page = WatchPage.PAIRING,
                        isConnecting = false,
                        comparisonCode = attempt.comparisonCode,
                        pairingMessage = "请在电脑端确认",
                        canReturnFromSettings = false,
                    )
                }
                val deadline = System.nanoTime() +
                    attempt.expiresInSeconds.coerceIn(1, 600) * 1_000_000_000L
                while (System.nanoTime() < deadline) {
                    delay(PAIRING_POLL_INTERVAL_MS)
                    val status = try {
                        repository.pollPairing(attempt)
                    } catch (error: IOException) {
                        _state.update { it.copy(pairingMessage = "网络有延迟，继续等待电脑确认") }
                        continue
                    }
                    when (status) {
                        PairingStatus.PENDING -> Unit
                        PairingStatus.APPROVED -> {
                            generation += 1
                            _state.update {
                                it.copy(
                                    page = WatchPage.RECOMMENDATIONS,
                                    comparisonCode = null,
                                    pairingMessage = null,
                                    canReturnFromSettings = false,
                                )
                            }
                            loadRecommendations()
                            return@launch
                        }
                        PairingStatus.REJECTED -> throw IOException("电脑端已拒绝配对")
                        PairingStatus.EXPIRED -> throw IOException("配对已过期，请重试")
                    }
                }
                throw IOException("配对已过期，请重试")
            } catch (error: CancellationException) {
                throw error
            } catch (error: Throwable) {
                _state.update {
                    it.copy(
                        page = WatchPage.CONNECTION,
                        isConnecting = false,
                        comparisonCode = null,
                        pairingMessage = null,
                        canReturnFromSettings = false,
                        connectionError = friendlyMessage(error),
                    )
                }
            }
        }
    }

    fun cancelPairing() {
        connectionJob?.cancel()
        connectionJob = null
        _state.update {
            it.copy(
                page = WatchPage.CONNECTION,
                isConnecting = false,
                comparisonCode = null,
                pairingMessage = null,
                canReturnFromSettings = false,
            )
        }
    }

    fun openSettings() {
        cancelPrepared()
        cancelCurrentTail(markSettled = true)
        val address = repository.savedConnection()?.let { ServerAddress.hostPort(it.baseUrl) }
        _state.update {
            it.copy(
                page = WatchPage.CONNECTION,
                host = address?.host ?: it.host,
                port = (address?.port ?: it.port.toIntOrNull() ?: ServerAddress.DEFAULT_PORT).toString(),
                connectionError = null,
                canReturnFromSettings = repository.hasToken(),
            )
        }
    }

    fun closeSettings() {
        if (!repository.hasToken()) return
        _state.update {
            it.copy(
                page = WatchPage.RECOMMENDATIONS,
                connectionError = null,
                canReturnFromSettings = false,
            )
        }
        maybePrepareNext()
    }

    fun loadRecommendations() {
        if (_state.value.recommendations.isLoading) return
        val currentGeneration = generation
        val prefetched = preparedPreload
        preparedPreload = null
        preparedMonitor?.cancel()
        preparedMonitor = null
        cancelCurrentTail(markSettled = false)
        recommendationJob?.cancel()
        recommendationJob = viewModelScope.launch {
            _state.update {
                it.copy(
                    page = WatchPage.RECOMMENDATIONS,
                    recommendations = it.recommendations.copy(
                        isLoading = true,
                        errorMessage = null,
                    ),
                )
            }
            var preload = prefetched ?: startPreload(currentGeneration)
            try {
                var response = try {
                    preload.awaitCritical()
                } catch (error: Throwable) {
                    if (error is CancellationException) throw error
                    if (prefetched == null || currentGeneration != generation) throw error
                    preload.cancel()
                    preload = startPreload(currentGeneration)
                    preload.awaitCritical()
                }
                if (currentGeneration != generation) {
                    preload.cancel()
                    return@launch
                }
                if (response.requestId != preload.requestId) {
                    throw IOException("推荐响应与请求不一致")
                }
                response = response.copy(items = response.items.take(RECOMMENDATION_COUNT))
                if (response.items.isEmpty()) throw IOException("服务器没有返回推荐图片")
                currentPreload = preload
                settledBatchId = null
                shownBatchId = null
                _state.update {
                    it.copy(
                        recommendations = RecommendationUiState(
                            batchId = response.batchId,
                            items = response.items,
                            isLoading = false,
                            errorMessage = if (response.items.size < RECOMMENDATION_COUNT) {
                                "本批只有 ${response.items.size} 张可用图片"
                            } else {
                                null
                            },
                        ),
                    )
                }
                monitorCurrentPreload(preload, response.batchId)
                markCurrentBatchShown(response.batchId)
            } catch (error: CancellationException) {
                preload.cancel()
                throw error
            } catch (error: Throwable) {
                preload.cancel()
                if (isAuthenticationError(error)) {
                    repository.clearToken()
                    _state.update {
                        it.copy(
                            page = WatchPage.CONNECTION,
                            recommendations = it.recommendations.copy(isLoading = false),
                            connectionError = "连接已失效，请重新配对",
                        )
                    }
                } else {
                    _state.update {
                        it.copy(
                            recommendations = it.recommendations.copy(
                                isLoading = false,
                                errorMessage = friendlyMessage(error),
                            ),
                        )
                    }
                }
            }
        }
    }

    fun openOriginal(itemId: String) {
        val item = _state.value.recommendations.items.firstOrNull { it.itemId == itemId } ?: return
        cancelPrepared()
        cancelCurrentTail(markSettled = true)
        _state.update { it.copy(page = WatchPage.ORIGINAL, selectedItem = item) }
        val batchId = _state.value.recommendations.batchId ?: return
        viewModelScope.launch {
            runCatching { repository.recordOpen(batchId, item.itemId, UUID.randomUUID().toString()) }
        }
    }

    fun closeOriginal() {
        _state.update { it.copy(page = WatchPage.RECOMMENDATIONS, selectedItem = null) }
        val recommendations = _state.value.recommendations
        recommendations.batchId?.let { batchId ->
            startOriginalPreload(batchId, recommendations.items)
        }
        maybePrepareNext()
    }

    private fun startPreload(currentGeneration: Long): RecommendationPreloadTask {
        val requestId = UUID.randomUUID().toString()
        return viewModelScope.startRecommendationPreload(
            requestId = requestId,
            generation = currentGeneration,
            criticalCount = CRITICAL_THUMBNAIL_COUNT,
            create = { generatedRequestId -> repository.recommendations(generatedRequestId) },
            preload = thumbnailLoader::preload,
        )
    }

    private fun monitorCurrentPreload(task: RecommendationPreloadTask, batchId: String) {
        currentMonitor?.cancel()
        currentMonitor = viewModelScope.launch {
            try {
                val outcome = task.awaitSettled()
                if (_state.value.recommendations.batchId != batchId) return@launch
                currentPreload = null
                settledBatchId = batchId
                if (outcome.failedCount > 0) {
                    _state.update {
                        it.copy(
                            recommendations = it.recommendations.copy(
                                errorMessage = "部分缩略图加载较慢，可点图直接查看原图",
                            ),
                        )
                    }
                }
                startOriginalPreload(batchId, _state.value.recommendations.items)
                maybePrepareNext()
            } catch (_: CancellationException) {
                Unit
            } catch (_: Throwable) {
                currentPreload = null
            }
        }
    }

    private fun markCurrentBatchShown(batchId: String) {
        shownJob?.cancel()
        val eventId = UUID.randomUUID().toString()
        shownJob = viewModelScope.launch {
            repeat(SHOWN_RETRY_COUNT) { attempt ->
                try {
                    repository.markShown(batchId, eventId)
                    if (_state.value.recommendations.batchId == batchId) {
                        shownBatchId = batchId
                        maybePrepareNext()
                    }
                    return@launch
                } catch (error: CancellationException) {
                    throw error
                } catch (_: Throwable) {
                    if (attempt + 1 < SHOWN_RETRY_COUNT) delay((attempt + 1) * 1_000L)
                }
            }
        }
    }

    private fun maybePrepareNext() {
        val recommendations = _state.value.recommendations
        val batchId = recommendations.batchId ?: return
        if (
            _state.value.page != WatchPage.RECOMMENDATIONS ||
            recommendations.isLoading ||
            recommendations.items.isEmpty() ||
            settledBatchId != batchId ||
            shownBatchId != batchId ||
            preparedPreload != null ||
            recommendationJob?.isActive == true
        ) {
            return
        }
        val task = startPreload(generation)
        preparedPreload = task
        preparedMonitor = viewModelScope.launch {
            try {
                val outcome = task.awaitSettled()
                if (outcome.failedCount > 0 && preparedPreload === task) {
                    preparedPreload = null
                    task.cancel()
                }
            } catch (_: CancellationException) {
                Unit
            } catch (_: Throwable) {
                if (preparedPreload === task) preparedPreload = null
            }
        }
    }

    private fun cancelPrepared() {
        preparedMonitor?.cancel()
        preparedMonitor = null
        preparedPreload?.cancel()
        preparedPreload = null
    }

    private fun startOriginalPreload(
        batchId: String,
        items: List<RecommendationItem>,
    ) {
        originalPreloadJob?.cancel()
        originalPreloadJob = viewModelScope.launch {
            for (item in items) {
                if (_state.value.recommendations.batchId != batchId) return@launch
                try {
                    originalLoader.preload(item.previewUrl)
                } catch (error: CancellationException) {
                    throw error
                } catch (_: Throwable) {
                    Unit
                }
            }
        }
    }

    private fun cancelCurrentTail(markSettled: Boolean) {
        currentMonitor?.cancel()
        currentMonitor = null
        currentPreload?.cancel()
        currentPreload = null
        originalPreloadJob?.cancel()
        originalPreloadJob = null
        if (markSettled) settledBatchId = _state.value.recommendations.batchId
    }

    private fun isAuthenticationError(error: Throwable): Boolean =
        error is ApiException && error.httpStatus in setOf(401, 403)

    private fun friendlyMessage(error: Throwable): String = when {
        error is ApiException && error.httpStatus == 502 ->
            "电脑端暂未响应，请确认电脑端已启动后重试"
        error is SocketTimeoutException ->
            "服务器响应较慢，请重试；当前内容会保留"
        error is ApiException && error.message.isNotBlank() -> error.message
        error is IOException -> "连接中断，请重试"
        else -> "操作失败，请重试"
    }

    companion object {
        private const val RECOMMENDATION_COUNT = 5
        private const val CRITICAL_THUMBNAIL_COUNT = 2
        private const val SHOWN_RETRY_COUNT = 3
        private const val PAIRING_POLL_INTERVAL_MS = 1_000L
    }
}

class WatchViewModelFactory(
    private val repository: WatchRepository,
    private val thumbnailLoader: ThumbnailLoader,
    private val originalLoader: OriginalLoader,
) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        if (modelClass.isAssignableFrom(WatchViewModel::class.java)) {
            return WatchViewModel(repository, thumbnailLoader, originalLoader) as T
        }
        throw IllegalArgumentException("Unknown ViewModel class")
    }
}
