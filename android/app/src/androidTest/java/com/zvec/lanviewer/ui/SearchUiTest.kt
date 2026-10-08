package com.zvec.lanviewer.ui

import android.content.res.Configuration
import android.graphics.Bitmap
import android.net.Uri
import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.longClick
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToIndex
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.unit.dp
import com.zvec.lanviewer.data.model.OriginalMediaItem
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.SearchMode
import com.zvec.lanviewer.ui.search.NEXT_PAGE_RETRY_TAG
import com.zvec.lanviewer.ui.search.QueryImageControl
import com.zvec.lanviewer.ui.search.SEARCH_RESULTS_GRID_TAG
import kotlinx.collections.immutable.PersistentList
import kotlinx.collections.immutable.toPersistentList
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList

class SearchUiTest {
    @get:Rule
    val composeRule = createAndroidComposeRule<ComponentActivity>()

    private val stateHolder = mutableStateOf(AppUiState())
    private val selectedTabHolder = mutableStateOf(MobileTab.RESULTS)
    private val snackbarHolder = SnackbarHostState()
    private val screenshotDark = mutableStateOf(false)
    private var gridStateRef: LazyGridState? = null

    @Test
    fun firstPageFailureIsVisibleOnTheSearchPageAndCanBeRetried() {
        stateHolder.value = AppUiState(
            phase = ConnectionPhase.READY,
            searchText = "cat",
            searchPhase = SearchPhase.ERROR,
            searchError = "电脑暂时无法完成搜索",
        )
        selectedTabHolder.value = MobileTab.SEARCH
        var retries = 0
        setHarness(onSearch = { retries += 1 })
        composeRule.onNodeWithText("电脑暂时无法完成搜索").assertExists()
        composeRule.onNodeWithText("重试").performClick()
        assertEquals(1, retries)
    }

    @Test
    fun searchGridPositionSurvivesViewerAndTabSwitch() {
        stateHolder.value = loadedState(searchItems(120))
        setHarness(
            onOpen = { stateHolder.value = stateHolder.value.copy(viewerSource = ViewerSource.SEARCH, viewerIndex = it) },
            onSelectedTab = { selectedTabHolder.value = it },
        )

        composeRule.onNodeWithTag(SEARCH_RESULTS_GRID_TAG).performScrollToIndex(60)
        val scrolled = composeRule.runOnIdle { requireNotNull(gridStateRef).firstVisibleItemIndex }
        assertTrue("需先滚动到后段", scrolled > 0)

        composeRule.onNodeWithText("item-60.jpg").performClick()
        composeRule.onNodeWithTag(ORIGINAL_VIEWER_IMAGE_TAG).assertExists()
        composeRule.runOnIdle { stateHolder.value = stateHolder.value.copy(viewerSource = null, viewerIndex = null) }
        composeRule.waitForIdle()
        assertEquals(scrolled, composeRule.runOnIdle { requireNotNull(gridStateRef).firstVisibleItemIndex })

        composeRule.onNodeWithText("推荐").performClick()
        composeRule.waitForIdle()
        composeRule.onNodeWithText("结果").performClick()
        composeRule.waitForIdle()
        assertEquals(scrolled, composeRule.runOnIdle { requireNotNull(gridStateRef).firstVisibleItemIndex })
    }

    @Test
    fun searchGridPositionSurvivesStateRestoration() {
        stateHolder.value = loadedState(searchItems(120))
        val restorationTester = StateRestorationTester(composeRule)
        restorationTester.setContent { HarnessContent() }

        composeRule.onNodeWithTag(SEARCH_RESULTS_GRID_TAG).performScrollToIndex(60)
        val scrolled = composeRule.runOnIdle { requireNotNull(gridStateRef).firstVisibleItemIndex }
        assertTrue(scrolled > 0)

        restorationTester.emulateSavedInstanceStateRestore()
        composeRule.waitForIdle()

        assertEquals(scrolled, composeRule.runOnIdle { requireNotNull(gridStateRef).firstVisibleItemIndex })
    }

    @Test
    fun newSearchIdentityResetsSearchGrid() {
        val items = searchItems(120)
        stateHolder.value = loadedState(items).copy(searchAttempt = 1)
        setHarness()
        composeRule.onNodeWithTag(SEARCH_RESULTS_GRID_TAG).performScrollToIndex(60)
        assertTrue(composeRule.runOnIdle { requireNotNull(gridStateRef).firstVisibleItemIndex } > 0)

        composeRule.runOnIdle { stateHolder.value = loadedState(items).copy(searchAttempt = 2) }
        composeRule.waitUntil(timeoutMillis = 5_000) {
            requireNotNull(gridStateRef).firstVisibleItemIndex == 0
        }

        assertEquals(0, composeRule.runOnIdle { requireNotNull(gridStateRef).firstVisibleItemIndex })
    }

    @Test
    fun searchResultPhasesAreDistinct() {
        stateHolder.value = AppUiState(phase = ConnectionPhase.READY, searchPhase = SearchPhase.IDLE)
        setHarness()
        composeRule.onNodeWithText("搜索后在这里查看结果").assertExists()

        composeRule.runOnIdle {
            stateHolder.value = AppUiState(
                phase = ConnectionPhase.READY,
                searchPhase = SearchPhase.SEARCHING,
                isSearching = true,
                searchStatus = "正在创建搜索…",
            )
        }
        composeRule.waitForIdle()
        composeRule.onNodeWithText("正在创建搜索…").assertExists()
        composeRule.onNodeWithText("搜索后在这里查看结果").assertDoesNotExist()

        composeRule.runOnIdle {
            stateHolder.value = AppUiState(
                phase = ConnectionPhase.READY,
                searchPhase = SearchPhase.LOADED,
                activeSearchId = "search-1",
            )
        }
        composeRule.waitForIdle()
        composeRule.onNodeWithText("没有找到匹配图片").assertExists()
        composeRule.onNodeWithText("修改搜索").assertExists()

        composeRule.runOnIdle {
            stateHolder.value = AppUiState(
                phase = ConnectionPhase.READY,
                searchPhase = SearchPhase.ERROR,
                searchError = "网络错误",
            )
        }
        composeRule.waitForIdle()
        composeRule.onNodeWithText("搜索失败").assertExists()
        composeRule.onNodeWithText("网络错误").assertExists()
        composeRule.onNodeWithText("重试").assertExists()

        composeRule.runOnIdle {
            stateHolder.value = AppUiState(phase = ConnectionPhase.READY, searchPhase = SearchPhase.CANCELLED)
        }
        composeRule.waitForIdle()
        composeRule.onNodeWithText("已取消").assertExists()
    }

    @Test
    fun nextPageErrorIsVisibleAndRetryKeepsOldItems() {
        var retryCount = 0
        stateHolder.value = loadedState(searchItems(2)).copy(
            totalResults = 120,
            nextPageError = "网络超时",
        )
        setHarness(onRetryPage = { retryCount += 1 })

        composeRule.onNodeWithText("下一页加载失败").assertExists()
        composeRule.onNodeWithText("网络超时").assertExists()
        composeRule.onNodeWithContentDescription("item-0.jpg").assertExists()

        composeRule.onNodeWithTag(NEXT_PAGE_RETRY_TAG).performClick()
        assertEquals(1, retryCount)
    }

    @Test
    fun uploadFailureNeverShowsCompletedAndOffersRetry() {
        var retryCount = 0
        composeRule.setContent {
            ZvecTheme {
                QueryImageControl(
                    query = QueryImageUiState(
                        uri = Uri.parse("content://test/query-1"),
                        displayName = "query.jpg",
                        mimeType = "image/jpeg",
                        queryImageId = null,
                        uploading = false,
                        uploadError = "网络错误",
                    ),
                    onPickImage = {},
                    onCancelUpload = {},
                    onRetryUpload = { retryCount += 1 },
                    onClearImage = {},
                )
            }
        }

        composeRule.onNodeWithText("上传完成").assertDoesNotExist()
        composeRule.onNodeWithText("上传失败：网络错误").assertExists()
        composeRule.onNodeWithText("移除").assertExists()
        composeRule.onNodeWithText("重试").performClick()
        assertEquals(1, retryCount)
    }

    @Test
    fun uploadShowsCompletedOnlyWithServerId() {
        composeRule.setContent {
            ZvecTheme {
                QueryImageControl(
                    query = QueryImageUiState(
                        uri = Uri.parse("content://test/query-1"),
                        displayName = "query.jpg",
                        mimeType = "image/jpeg",
                        queryImageId = "server-1",
                        uploading = false,
                    ),
                    onPickImage = {},
                    onCancelUpload = {},
                    onRetryUpload = {},
                    onClearImage = {},
                )
            }
        }
        composeRule.onNodeWithText("上传完成").assertExists()
        composeRule.onNodeWithText("上传失败：网络错误").assertDoesNotExist()
    }

    @Test
    fun uploadingStateShowsProgressWithoutCompleted() {
        composeRule.setContent {
            ZvecTheme {
                QueryImageControl(
                    query = QueryImageUiState(
                        uri = Uri.parse("content://test/query-1"),
                        displayName = "query.jpg",
                        mimeType = "image/jpeg",
                        uploading = true,
                        bytesUploaded = 2048,
                        totalBytes = 4096,
                    ),
                    onPickImage = {},
                    onCancelUpload = {},
                    onRetryUpload = {},
                    onClearImage = {},
                )
            }
        }
        composeRule.onNodeWithText("上传完成").assertDoesNotExist()
        composeRule.onNodeWithText("上传中", substring = true).assertExists()
    }

    @Test
    fun gridUsesThumbnailCallbackAndViewerUsesOriginalCallback() {
        val thumbnailCalls = CopyOnWriteArrayList<SearchItem>()
        val originalCalls = CopyOnWriteArrayList<OriginalMediaItem>()
        stateHolder.value = loadedState(searchItems(6))
        setHarness(
            thumbnailUrl = { item -> thumbnailCalls += item; TEST_IMAGE_URL },
            originalUrl = { item -> originalCalls += item; TEST_IMAGE_URL },
        )
        composeRule.waitForIdle()

        assertTrue("网格必须使用缩略图回调", thumbnailCalls.isNotEmpty())
        assertTrue("网格不得请求原图", originalCalls.isEmpty())

        composeRule.runOnIdle {
            stateHolder.value = stateHolder.value.copy(viewerSource = ViewerSource.SEARCH, viewerIndex = 0)
        }
        composeRule.waitForIdle()
        composeRule.onNodeWithTag(ORIGINAL_VIEWER_IMAGE_TAG).assertExists()
        assertTrue("查看器必须使用原图回调", originalCalls.isNotEmpty())
    }

    @Test
    fun transferBusyDisablesSaveShareAndStaysVisibleInList() {
        stateHolder.value = loadedState(searchItems(1)).copy(
            viewerSource = ViewerSource.SEARCH,
            viewerIndex = 0,
            transferAttempt = 1L,
            transferMessage = "正在保存 item-0.jpg",
            transferFraction = 0.25f,
        )
        setHarness()

        composeRule.onNodeWithText("正在保存 item-0.jpg").assertExists()
        composeRule.onNodeWithText("取消").assertExists()
        composeRule.onAllNodesWithTag(APP_SNACKBAR_HOST_TAG, useUnmergedTree = true).assertCountEquals(1)

        composeRule.onNodeWithTag(ORIGINAL_VIEWER_IMAGE_TAG).performTouchInput { longClick() }
        composeRule.onNodeWithText("保存").assertIsNotEnabled()
        composeRule.onNodeWithText("分享").assertIsNotEnabled()

        composeRule.runOnIdle { stateHolder.value = stateHolder.value.copy(viewerSource = null, viewerIndex = null) }
        composeRule.waitForIdle()
        composeRule.onNodeWithText("正在保存 item-0.jpg").assertExists()
        composeRule.onNodeWithText("取消").assertExists()
        composeRule.onAllNodesWithTag(APP_SNACKBAR_HOST_TAG, useUnmergedTree = true).assertCountEquals(1)
    }

    @Test
    fun exportSearchEmptyStateScreenshots() {
        stateHolder.value = AppUiState(phase = ConnectionPhase.READY, searchPhase = SearchPhase.IDLE)
        setScreenshotContent()
        captureScreenshot("search-empty-light-320.png")
        composeRule.runOnIdle { screenshotDark.value = true }
        captureScreenshot("search-empty-dark-320.png")
    }

    @Test
    fun exportSearchUploadFailureScreenshots() {
        selectedTabHolder.value = MobileTab.SEARCH
        stateHolder.value = AppUiState(
            phase = ConnectionPhase.READY,
            searchMode = SearchMode.TEXT,
            searchText = "日落",
            queryImage = QueryImageUiState(
                uri = Uri.parse("content://test/query-1"),
                displayName = "query.jpg",
                mimeType = "image/jpeg",
                queryImageId = null,
                uploading = false,
                uploadError = "网络错误",
            ),
        )
        setScreenshotContent()
        captureScreenshot("search-upload-failure-light-320.png")
        composeRule.runOnIdle { screenshotDark.value = true }
        captureScreenshot("search-upload-failure-dark-320.png")
    }

    @Test
    fun exportSearchPageErrorScreenshots() {
        stateHolder.value = loadedState(searchItems(2)).copy(totalResults = 120, nextPageError = "网络超时")
        setScreenshotContent()
        captureScreenshot("search-page-error-light-320.png")
        composeRule.runOnIdle { screenshotDark.value = true }
        captureScreenshot("search-page-error-dark-320.png")
    }

    @Test
    fun exportViewerTransferScreenshots() {
        stateHolder.value = loadedState(searchItems(1)).copy(
            viewerSource = ViewerSource.SEARCH,
            viewerIndex = 0,
            transferAttempt = 1L,
            transferMessage = "正在保存 item-0.jpg",
            transferFraction = 0.4f,
        )
        setScreenshotContent()
        captureScreenshot("viewer-transfer-light-320.png")
        composeRule.runOnIdle { screenshotDark.value = true }
        captureScreenshot("viewer-transfer-dark-320.png")
    }

    private fun setHarness(
        thumbnailUrl: (SearchItem) -> String = { TEST_IMAGE_URL },
        originalUrl: (OriginalMediaItem) -> String = { TEST_IMAGE_URL },
        onRetryPage: () -> Unit = {},
        onRetrySearch: () -> Unit = {},
        onSearch: () -> Unit = {},
        onOpen: (Int) -> Unit = {},
        onCloseViewer: () -> Unit = {},
        onSelectedTab: (MobileTab) -> Unit = {},
    ) {
        composeRule.setContent {
            HarnessContent(
                thumbnailUrl = thumbnailUrl,
                originalUrl = originalUrl,
                onRetryPage = onRetryPage,
                onRetrySearch = onRetrySearch,
                onSearch = onSearch,
                onOpen = onOpen,
                onCloseViewer = onCloseViewer,
                onSelectedTab = onSelectedTab,
            )
        }
    }

    private fun setScreenshotContent() {
        composeRule.setContent {
            val base = LocalConfiguration.current
            val dark = screenshotDark.value
            val config = remember(base, dark) {
                val nightMode = if (dark) Configuration.UI_MODE_NIGHT_YES else Configuration.UI_MODE_NIGHT_NO
                Configuration(base).apply {
                    uiMode = (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or nightMode
                }
            }
            CompositionLocalProvider(LocalConfiguration provides config) {
                ZvecTheme {
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Surface(
                            modifier = Modifier.width(320.dp).fillMaxHeight().testTag(SCREENSHOT_TAG),
                            color = MaterialTheme.colorScheme.background,
                        ) {
                            HarnessContent()
                        }
                    }
                }
            }
        }
    }

    private fun captureScreenshot(name: String) {
        composeRule.waitForIdle()
        val output = File(composeRule.activity.filesDir, name)
        output.outputStream().use { stream ->
            composeRule.onNodeWithTag(SCREENSHOT_TAG).captureToImage().asAndroidBitmap()
                .compress(Bitmap.CompressFormat.PNG, 100, stream)
        }
    }

    @Composable
    private fun HarnessContent(
        thumbnailUrl: (SearchItem) -> String = { TEST_IMAGE_URL },
        originalUrl: (OriginalMediaItem) -> String = { TEST_IMAGE_URL },
        onRetryPage: () -> Unit = {},
        onRetrySearch: () -> Unit = {},
        onSearch: () -> Unit = {},
        onOpen: (Int) -> Unit = {},
        onCloseViewer: () -> Unit = {},
        onSelectedTab: (MobileTab) -> Unit = {},
    ) {
        ZvecTheme {
            gridStateRef = rememberLazyGridState()
            SearchTestHost(
                state = stateHolder.value,
                selectedTab = selectedTabHolder.value,
                gridState = requireNotNull(gridStateRef),
                snackbarHostState = snackbarHolder,
                thumbnailUrl = thumbnailUrl,
                originalUrl = originalUrl,
                onSelectedTab = onSelectedTab,
                onOpen = onOpen,
                onCloseViewer = onCloseViewer,
                onRetryPage = onRetryPage,
                onRetrySearch = onRetrySearch,
                onSearch = onSearch,
            )
        }
    }
}

@Composable
private fun SearchTestHost(
    state: AppUiState,
    selectedTab: MobileTab,
    gridState: LazyGridState,
    snackbarHostState: SnackbarHostState,
    thumbnailUrl: (SearchItem) -> String,
    originalUrl: (OriginalMediaItem) -> String,
    onSelectedTab: (MobileTab) -> Unit = {},
    onOpen: (Int) -> Unit = {},
    onCloseViewer: () -> Unit = {},
    onLoadMore: () -> Unit = {},
    onRetryPage: () -> Unit = {},
    onRetrySearch: () -> Unit = {},
    onSearch: () -> Unit = {},
) {
    // Mirrors the production wiring: the search grid is lifted above the viewer and anchored to the
    // current search identity, and a single SnackbarHost is shared by the viewer and the list.
    SearchGridAnchorEffect(gridState = gridState, searchAttempt = state.searchAttempt)
    Box(Modifier.fillMaxSize()) {
        val viewerOpen = state.viewerIndex != null && state.viewerItems().isNotEmpty()
        if (viewerOpen) {
            OriginalViewer(
                state = state,
                mediaUrl = originalUrl,
                onIndexChanged = {},
                onClose = onCloseViewer,
                onSave = {},
                onShare = {},
                onCancelTransfer = {},
                onReaction = { _, _ -> },
            )
        } else {
            SearchScreen(
                state = state,
                selectedTab = selectedTab,
                searchGridState = gridState,
                recommendationGridState = rememberLazyGridState(),
                modifier = Modifier.fillMaxSize(),
                thumbnailUrl = thumbnailUrl,
                transferMessage = state.transferMessage,
                transferFraction = state.transferFraction,
                onCancelTransfer = {},
                onSelectedTab = onSelectedTab,
                onMode = {},
                onText = {},
                onTopK = {},
                onLibrary = {},
                onPickImage = {},
                onCancelUpload = {},
                onRetryUpload = {},
                onClearImage = {},
                onSearch = onSearch,
                onCancelSearch = {},
                onRetrySearch = onRetrySearch,
                onLoadMore = onLoadMore,
                onRetryPage = onRetryPage,
                onOpen = onOpen,
                onLoadRecommendations = {},
                onRecommendationsVisible = {},
                onOpenRecommendation = {},
                onDisconnect = {},
            )
        }
        SnackbarHost(
            hostState = snackbarHostState,
            modifier = Modifier.align(Alignment.BottomCenter).testTag(APP_SNACKBAR_HOST_TAG),
        )
    }
}

private fun searchItems(count: Int): PersistentList<SearchItem> = (0 until count).map { index ->
    SearchItem(
        mediaId = "media-$index",
        name = "item-$index.jpg",
        width = 640,
        height = 480,
        libraryName = "图库",
    )
}.toPersistentList()

private fun loadedState(items: PersistentList<SearchItem>): AppUiState = AppUiState(
    phase = ConnectionPhase.READY,
    activeSearchId = "search-1",
    searchPhase = SearchPhase.LOADED,
    results = items,
    totalResults = items.size,
    nextPage = 2,
)

private const val SCREENSHOT_TAG = "search-screenshot"
private const val TEST_IMAGE_URL = "android.resource://com.zvec.lanviewer/mipmap/ic_launcher"
