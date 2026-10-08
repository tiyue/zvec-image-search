package com.zvec.lanviewer.ui

import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.SearchMode
import com.zvec.lanviewer.data.model.SearchPageResponse
import com.zvec.lanviewer.ui.search.searchSummary
import kotlinx.collections.immutable.persistentListOf
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

class SearchSessionStateTest {
    @Test
    fun firstPageLoadAppendsResultsAndRaisesCompletionSignalOnce() {
        val searching = AppUiState(
            searchPhase = SearchPhase.SEARCHING,
            searchAttempt = 1,
            activeSearchId = "s1",
            isSearching = true,
        )

        val loaded = searching.applyPageLoaded(1, page(page = 1, total = 40, count = 10), requestedPage = 1)

        assertEquals(SearchPhase.LOADED, loaded.searchPhase)
        assertFalse(loaded.isSearching)
        assertEquals(10, loaded.results.size)
        assertEquals(40, loaded.totalResults)
        assertEquals(2, loaded.nextPage)
        assertEquals(1L, loaded.searchCompletionSignal)

        assertSame(
            loaded,
            loaded.applyPageLoaded(1, page(page = 1, total = 40, count = 10), requestedPage = 1),
        )

        val second = loaded.applyPageLoaded(1, page(page = 2, total = 40, count = 10), requestedPage = 2)
        assertEquals(20, second.results.size)
        assertEquals(1L, second.searchCompletionSignal)
    }

    @Test
    fun eachNewSearchRaisesItsOwnSingleCompletionSignal() {
        val first = AppUiState(searchPhase = SearchPhase.SEARCHING, searchAttempt = 1, activeSearchId = "s1")
            .applyPageLoaded(1, page(page = 1, total = 5, count = 5), requestedPage = 1)

        val next = first.beginSearchSession(attempt = 2)
        assertEquals(0, next.results.size)
        assertEquals(1L, next.searchCompletionSignal)

        val completed = next.applyPageLoaded(2, page(page = 1, total = 3, count = 3), requestedPage = 1)
        assertEquals(2L, completed.searchCompletionSignal)
    }

    @Test
    fun zeroResultCompletionLoadsWithoutAutomaticRepagination() {
        val loaded = AppUiState(searchPhase = SearchPhase.SEARCHING, searchAttempt = 1, activeSearchId = "s1")
            .applyPageLoaded(1, page(page = 1, total = 0, count = 0), requestedPage = 1)

        assertEquals(SearchPhase.LOADED, loaded.searchPhase)
        assertTrue(loaded.results.isEmpty())
        assertEquals(1L, loaded.searchCompletionSignal)
        assertFalse(loaded.canStartNextPage())
    }

    @Test
    fun commitsFromAReplacedSearchAreIgnored() {
        val replaced = AppUiState(
            searchPhase = SearchPhase.SEARCHING,
            searchAttempt = 2,
            activeSearchId = "s2",
            isSearching = true,
        )

        assertSame(replaced, replaced.applySearchCreated(attempt = 1, searchId = "s1"))
        assertSame(replaced, replaced.applyPageLoaded(1, page(page = 1, total = 5, count = 5), requestedPage = 1))
        assertSame(replaced, replaced.applyPageFailed(1, requestedPage = 1, message = "旧错误"))
        assertSame(replaced, replaced.clearNextPageError(attempt = 1))
    }

    @Test
    fun cancelRejectsLateSuccessErrorAndLoadingCallbacks() {
        val searching = AppUiState(
            searchPhase = SearchPhase.SEARCHING,
            searchAttempt = 1,
            activeSearchId = "s1",
            isSearching = true,
        )

        val cancelled = searching.cancelSearchSession(attempt = 2)

        assertEquals(SearchPhase.CANCELLED, cancelled.searchPhase)
        assertNull(cancelled.activeSearchId)
        assertFalse(cancelled.isSearching)
        assertSame(cancelled, cancelled.applySearchCreated(attempt = 1, searchId = "s1"))
        assertSame(cancelled, cancelled.applyPageLoaded(1, page(page = 1, total = 5, count = 5), requestedPage = 1))
        assertSame(cancelled, cancelled.applyPageFailed(1, requestedPage = 1, message = "旧错误"))
    }

    @Test
    fun beginSearchSessionClearsPreviousResultsPaginationAndViewer() {
        val previous = AppUiState(
            searchPhase = SearchPhase.LOADED,
            searchAttempt = 1,
            activeSearchId = "s1",
            isLoadingNextPage = true,
            nextPage = 3,
            results = persistentListOf(SearchItem(mediaId = "a"), SearchItem(mediaId = "b")),
            totalResults = 40,
            searchError = "旧失败",
            nextPageError = "旧翻页失败",
            searchRestoreNotice = "旧提示",
            viewerSource = ViewerSource.SEARCH,
            viewerIndex = 1,
        )

        val next = previous.beginSearchSession(attempt = 2)

        assertEquals(2L, next.searchAttempt)
        assertNull(next.activeSearchId)
        assertEquals(1, next.nextPage)
        assertTrue(next.results.isEmpty())
        assertEquals(0, next.totalResults)
        assertEquals(SearchPhase.SEARCHING, next.searchPhase)
        assertTrue(next.isSearching)
        assertFalse(next.isLoadingNextPage)
        assertNull(next.searchError)
        assertNull(next.nextPageError)
        assertNull(next.searchRestoreNotice)
        assertNull(next.viewerSource)
        assertNull(next.viewerIndex)
    }

    @Test
    fun pageErrorBlocksAutomaticRetryUntilItIsExplicitlyCleared() {
        val loaded = AppUiState(searchPhase = SearchPhase.SEARCHING, searchAttempt = 1, activeSearchId = "s1")
            .applyPageLoaded(1, page(page = 1, total = 40, count = 10), requestedPage = 1)
        assertTrue(loaded.canStartNextPage())

        val failed = loaded.applyPageFailed(1, requestedPage = 2, message = "网络超时")

        assertEquals(SearchPhase.LOADED, failed.searchPhase)
        assertEquals("网络超时", failed.nextPageError)
        assertEquals(10, failed.results.size)
        assertFalse(failed.canStartNextPage())

        val cleared = failed.clearNextPageError(attempt = 1)
        assertNull(cleared.nextPageError)
        assertTrue(cleared.canStartNextPage())
    }

    @Test
    fun firstPageFailureEntersErrorStateAndBlocksPaging() {
        val searching = AppUiState(
            searchPhase = SearchPhase.SEARCHING,
            searchAttempt = 1,
            activeSearchId = "s1",
            isSearching = true,
        )

        val failed = searching.applyPageFailed(1, requestedPage = 1, message = "首屏失败")

        assertEquals(SearchPhase.ERROR, failed.searchPhase)
        assertEquals("首屏失败", failed.searchError)
        assertNull(failed.nextPageError)
        assertFalse(failed.canStartNextPage())
    }

    @Test
    fun validationKeepsExistingModeAndNumberRules() {
        assertEquals(
            SearchRequestValidation.Valid(SearchMode.TEXT, 36),
            AppUiState(searchMode = SearchMode.TEXT, searchText = "cat", topKText = "36").validateSearchRequest(),
        )
        assertEquals(
            SearchRequestValidation.Valid(SearchMode.TAG, 10),
            AppUiState(searchMode = SearchMode.TAG, searchText = "flower", topKText = "10").validateSearchRequest(),
        )
        assertEquals(
            "请输入搜索文字",
            invalidMessage(AppUiState(searchMode = SearchMode.TEXT, searchText = "   ", topKText = "36")),
        )
        assertEquals(
            "结果数量必须是正整数",
            invalidMessage(AppUiState(searchMode = SearchMode.TEXT, searchText = "cat", topKText = "0")),
        )
        assertEquals(
            "结果数量必须是正整数",
            invalidMessage(AppUiState(searchMode = SearchMode.TEXT, searchText = "cat", topKText = "")),
        )
    }

    @Test
    fun attachedQueryImageErrorsKeepUploadingAndFailedStatesExplicit() {
        assertEquals(
            "查询图片正在上传，请稍候再搜索",
            attachedQueryImageError(uploading = true, queryImageId = null, uploadError = null),
        )
        assertEquals(
            "上传失败",
            attachedQueryImageError(uploading = false, queryImageId = null, uploadError = "上传失败"),
        )
        assertEquals(
            "查询图片尚未上传完成，请重试或移除",
            attachedQueryImageError(uploading = false, queryImageId = null, uploadError = null),
        )
        assertNull(attachedQueryImageError(uploading = false, queryImageId = "q1", uploadError = null))
    }

    @Test
    fun completedQueryTitleDoesNotFollowDraftEdits() {
        val submitted = AppUiState(searchText = "日落").beginSearchSession(1L)
        val editingNextDraft = submitted.copy(searchText = "海边")
        assertEquals("日落", searchSummary(editingNextDraft))
    }

    @Test
    fun resubmittingCapturesTheNewSummarySnapshot() {
        val first = AppUiState(searchText = "日落").beginSearchSession(1L)
        assertEquals("日落", searchSummary(first))

        val second = first.copy(searchText = "海边").beginSearchSession(2L)
        assertEquals("海边", searchSummary(second))
    }

    @Test
    fun submittedImageOnlyQuerySummarizesAsImageSearch() {
        val imageOnly = AppUiState(submittedSearchText = "", submittedSearchHasImage = true)
        assertEquals("以图搜索", searchSummary(imageOnly))
    }

    private fun invalidMessage(state: AppUiState): String? =
        (state.validateSearchRequest() as? SearchRequestValidation.Invalid)?.message

    private fun page(page: Int, total: Int, count: Int): SearchPageResponse = SearchPageResponse(
        searchId = "s1",
        page = page,
        pageSize = SEARCH_PAGE_SIZE,
        total = total,
        items = (1..count).map { item("p$page-$it") },
    )

    private fun item(id: String): SearchItem = SearchItem(mediaId = id, name = "$id.jpg")
}
