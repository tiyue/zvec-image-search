package com.zvec.lanviewer.ui

import com.zvec.lanviewer.data.model.SearchMode
import com.zvec.lanviewer.data.model.SearchPageResponse
import kotlinx.collections.immutable.persistentListOf

/**
 * Explicit lifecycle of one local search attempt.
 *
 * Every commit carries the [AppUiState.searchAttempt] it belongs to, so a cancelled, replaced or
 * reconnected search can never let a stale success, error or loading result overwrite the newest
 * state. The attempt number is the minimal business identity check and stays valid across IO
 * callbacks because it lives inside the state.
 */
enum class SearchPhase {
    IDLE,
    SEARCHING,
    LOADED,
    ERROR,
    CANCELLED,
}

internal sealed interface SearchRequestValidation {
    data class Valid(val mode: SearchMode, val topK: Int) : SearchRequestValidation

    data class Invalid(val message: String) : SearchRequestValidation
}

/**
 * Rejects an invalid original request before any identity or server session changes. An attached but
 * still uploading or failed query image must never degrade silently to a text-only request.
 */
internal fun AppUiState.validateSearchRequest(): SearchRequestValidation {
    val topK = topKText.toIntOrNull()?.takeIf { it > 0 }
        ?: return SearchRequestValidation.Invalid("结果数量必须是正整数")
    val mode = resolvedSearchMode()
    when (mode) {
        SearchMode.TEXT, SearchMode.TAG -> if (searchText.isBlank()) {
            return SearchRequestValidation.Invalid("请输入搜索文字")
        }
        SearchMode.IMAGE, SearchMode.COMBINED -> {
            val query = queryImage ?: return SearchRequestValidation.Invalid("请先选择查询图片")
            attachedQueryImageError(
                uploading = query.uploading,
                queryImageId = query.queryImageId,
                uploadError = query.uploadError,
            )?.let { message ->
                return SearchRequestValidation.Invalid(message)
            }
            if (mode == SearchMode.COMBINED && searchText.isBlank()) {
                return SearchRequestValidation.Invalid("图文联合搜索还需要输入文字")
            }
        }
    }
    return SearchRequestValidation.Valid(mode, topK)
}

/**
 * Message for an attached query image that is not ready, or null when it can be searched. Kept
 * free of Android types so the upload-state rules stay unit testable on the JVM.
 */
internal fun attachedQueryImageError(
    uploading: Boolean,
    queryImageId: String?,
    uploadError: String?,
): String? = when {
    uploading -> "查询图片正在上传，请稍候再搜索"
    queryImageId == null -> uploadError ?: "查询图片尚未上传完成，请重试或移除"
    else -> null
}

/** Clears the previous search, pagination and viewer state and claims a fresh local identity. */
internal fun AppUiState.beginSearchSession(attempt: Long): AppUiState = copy(
    searchAttempt = attempt,
    activeSearchId = null,
    nextPage = 1,
    results = persistentListOf(),
    totalResults = 0,
    isSearching = true,
    isLoadingNextPage = false,
    searchStatus = "正在创建搜索…",
    searchPhase = SearchPhase.SEARCHING,
    errorMessage = null,
    searchError = null,
    nextPageError = null,
    searchRestoreNotice = null,
    viewerSource = null,
    viewerIndex = null,
)

internal fun AppUiState.applySearchCreated(attempt: Long, searchId: String): AppUiState =
    if (searchAttempt != attempt) {
        this
    } else {
        copy(
            activeSearchId = searchId,
            nextPage = 1,
            isSearching = true,
            searchStatus = "等待电脑完成搜索…",
        )
    }

/**
 * Appends one committed page. The first page (including a zero-result completion) advances
 * [AppUiState.searchCompletionSignal] exactly once so the UI can navigate on that single signal;
 * later pages never re-trigger it.
 */
internal fun AppUiState.applyPageLoaded(
    attempt: Long,
    response: SearchPageResponse,
    requestedPage: Int,
): AppUiState = if (searchAttempt != attempt || requestedPage != nextPage) {
    this
} else {
    copy(
        isLoadingNextPage = false,
        isSearching = false,
        searchStatus = null,
        searchPhase = SearchPhase.LOADED,
        searchError = null,
        nextPageError = null,
        results = results.addAll(response.items),
        totalResults = response.total,
        nextPage = response.page + 1,
        searchCompletionSignal = if (requestedPage == 1) {
            searchCompletionSignal + 1
        } else {
            searchCompletionSignal
        },
    )
}

/** First-page failures keep the draft and surface a retry; page failures keep the loaded results. */
internal fun AppUiState.applyPageFailed(attempt: Long, requestedPage: Int, message: String): AppUiState =
    if (searchAttempt != attempt) {
        this
    } else if (requestedPage <= 1) {
        copy(
            isLoadingNextPage = false,
            isSearching = false,
            searchStatus = null,
            searchPhase = SearchPhase.ERROR,
            searchError = message,
            nextPageError = null,
        )
    } else {
        copy(isLoadingNextPage = false, searchStatus = null, nextPageError = message)
    }

internal fun AppUiState.cancelSearchSession(attempt: Long): AppUiState = copy(
    searchAttempt = attempt,
    activeSearchId = null,
    isSearching = false,
    isLoadingNextPage = false,
    searchStatus = null,
    searchPhase = SearchPhase.CANCELLED,
    searchError = null,
    nextPageError = null,
)

internal fun AppUiState.clearNextPageError(attempt: Long): AppUiState =
    if (searchAttempt != attempt) this else copy(nextPageError = null)

/**
 * Gate for automatic pagination. A page error and a completed zero-result search both block an
 * immediate retry; only [AppUiState.clearNextPageError] plus an explicit user retry may resume.
 */
internal fun AppUiState.canStartNextPage(): Boolean {
    if (activeSearchId == null || isLoadingNextPage || nextPageError != null) return false
    return when (searchPhase) {
        SearchPhase.SEARCHING -> results.isEmpty() && totalResults == 0
        SearchPhase.LOADED -> results.isNotEmpty() && !(totalResults > 0 && results.size >= totalResults)
        SearchPhase.IDLE, SearchPhase.ERROR, SearchPhase.CANCELLED -> false
    }
}
