package com.zvec.lanviewer.ui

import android.content.res.Configuration
import android.graphics.Bitmap
import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.requiredSize
import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.click
import androidx.compose.ui.test.doubleClick
import androidx.compose.ui.test.isPopup
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasAnyDescendant
import androidx.compose.ui.test.hasContentDescription
import androidx.compose.ui.test.SemanticsNodeInteraction
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.longClick
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onFirst
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToIndex
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.requestFocus
import androidx.compose.ui.test.swipeLeft
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.test.espresso.Espresso.pressBack
import com.zvec.lanviewer.data.model.OriginalMediaItem
import com.zvec.lanviewer.data.model.RecommendationItem
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.SearchMode
import com.zvec.lanviewer.ui.search.GALLERY_CARD_TAG
import com.zvec.lanviewer.ui.search.SEARCH_RESULTS_GRID_TAG
import kotlinx.collections.immutable.persistentListOf
import kotlinx.collections.immutable.toPersistentList
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.Buffer

/**
 * Stage B native acceptance for the adaptive gallery, the search inputs and the viewer chrome.
 *
 * Every case renders the real [ZvecTheme] and the real pages inside a fixed dp container, so the
 * column count, the toolbar and the density persistence are checked against the actual layout
 * instead of a mirrored helper. Screenshots are written to the test app's filesDir for the
 * coordinator's visual check.
 */
class BrowseUiTest {
    @get:Rule
    val composeRule = createAndroidComposeRule<ComponentActivity>()

    private val stateHolder = mutableStateOf(AppUiState())
    private val selectedTabHolder = mutableStateOf(MobileTab.RESULTS)
    private val shotConfig = mutableStateOf(BrowseShot())
    private var contentSet = false
    private var gridStateRef: LazyGridState? = null
    private var searchCalls = 0
    private var viewerImageUrl = TEST_IMAGE_URL
    private var thumbnailUrlProvider: (OriginalMediaItem) -> String = { TEST_IMAGE_URL }
    private var mediaUrlProvider: (OriginalMediaItem) -> String = { viewerImageUrl }

    @Before
    fun resetLocalDensity() {
        val context = composeRule.activity.applicationContext
        GalleryDensityStore.save(context, GalleryDensity.STANDARD)
        searchCalls = 0
    }

    @Test
    fun columnsFollowTheContainerWidth() {
        listOf(320 to 2, 600 to 3, 840 to 5).forEach { (width, expected) ->
            renderBrowse(state = loadedState(120), widthDp = width, selectedTab = MobileTab.RESULTS)
            assertEquals("width $width", expected, visibleColumnCount())
        }
    }

    @Test
    fun compactDensityAddsAColumnAt360dp() {
        renderBrowse(state = loadedState(60), widthDp = 360, selectedTab = MobileTab.RESULTS)
        assertEquals(2, visibleColumnCount())

        selectDensity(GalleryDensity.COMPACT)
        assertEquals(3, visibleColumnCount())
    }

    @Test
    fun densityChangeKeepsTheImageAnchorPersistsAndNeverSearches() {
        val context = composeRule.activity.applicationContext
        renderBrowse(state = loadedState(120), widthDp = 320, selectedTab = MobileTab.RESULTS)

        composeRule.onNodeWithTag(SEARCH_RESULTS_GRID_TAG).performScrollToIndex(60)
        val anchor = composeRule.runOnIdle { requireNotNull(gridStateRef).firstVisibleItemIndex }
        assertTrue("需先滚动到后段", anchor > 0)

        selectDensity(GalleryDensity.LARGE)

        assertEquals(anchor, composeRule.runOnIdle { requireNotNull(gridStateRef).firstVisibleItemIndex })
        assertEquals(GalleryDensity.LARGE, GalleryDensityStore.load(context))
        assertEquals(0, searchCalls)
        assertEquals(1, visibleColumnCount())
    }

    @Test
    fun densitySelectionIsRestoredFromLocalPreferences() {
        val context = composeRule.activity.applicationContext
        GalleryDensityStore.save(context, GalleryDensity.COMPACT)

        renderBrowse(state = loadedState(30), widthDp = 360, selectedTab = MobileTab.RESULTS)

        composeRule.onNodeWithText("紧凑", substring = true).assertExists()
        assertEquals(3, visibleColumnCount())
    }

    @Test
    fun cardsAndRecommendationsKeepFeedbackOutOfTheGrid() {
        renderBrowse(state = recommendationState(), widthDp = 320, selectedTab = MobileTab.RECOMMENDATIONS)

        composeRule.onAllNodesWithTag(GALLERY_CARD_TAG).onFirst().assertExists()
        listOf("喜欢", "不喜欢", "已喜欢", "已不喜欢", "已跳过").forEach { label ->
            composeRule.onNodeWithText(label).assertDoesNotExist()
        }
        composeRule.onNodeWithContentDescription("recommended-0.jpg").assertExists()
    }

    @Test
    fun searchControlsStayReachableWithLargeFontsOnAShortPage() {
        renderBrowse(
            state = AppUiState(phase = ConnectionPhase.READY, searchMode = SearchMode.TEXT),
            widthDp = 360,
            heightDp = 320,
            fontScale = 1.5f,
            selectedTab = MobileTab.SEARCH,
        )

        composeRule.onNodeWithText("↑").assertExists()
        composeRule.onNodeWithText("搜索设置", substring = true).performScrollTo().performClick()
        composeRule.onNodeWithText("结果数量").assertExists()
        pressBack()
        composeRule.onNodeWithText("结果数量").assertDoesNotExist()
    }

    @Test
    fun searchInputFocusShowsImeAndSettingsDoneIsReachableOnAShortPage() {
        renderBrowse(
            state = AppUiState(phase = ConnectionPhase.READY, searchMode = SearchMode.TEXT),
            widthDp = 360,
            heightDp = 640,
            density = composeRule.activity.resources.displayMetrics.density,
            fontScale = 1.5f,
            selectedTab = MobileTab.SEARCH,
        )

        // The settings entry and its "完成" action stay scrollable and clickable on a short page.
        composeRule.onNodeWithText("搜索设置", substring = true).performScrollTo().performClick()
        composeRule.onNodeWithText("结果数量").assertExists()
        composeRule.onNodeWithText("完成").performScrollTo().performClick()
        composeRule.onNodeWithText("结果数量").assertDoesNotExist()

        // Real focus and a real IME: typing must update the state and the keyboard must actually show.
        val field = composeRule.onNodeWithContentDescription("搜索文字输入框")
        field.requestFocus()
        composeRule.waitForIdle()
        field.performTextInput("cat")
        composeRule.runOnIdle { assertEquals("cat", stateHolder.value.searchText) }
        assertTrue("聚焦输入框后软键盘必须显示", awaitImeVisible())
        composeRule.onNodeWithText("↑").performScrollTo().performClick()
        composeRule.runOnIdle { assertEquals(1, searchCalls) }
        composeRule.waitUntil(timeoutMillis = 5_000) {
            !ViewCompat.getRootWindowInsets(composeRule.activity.window.decorView)!!
                .isVisible(WindowInsetsCompat.Type.ime())
        }
        composeRule.onNodeWithText("搜索设置", substring = true).performScrollTo().performClick()
        composeRule.onNodeWithText("结果数量").performClick()
        assertTrue("设置输入也应显示软键盘", awaitImeVisible())
        composeRule.onNodeWithText("完成").performScrollTo().performClick()
        composeRule.onNodeWithText("结果数量").assertDoesNotExist()
    }

    @Test
    fun viewerToolbarTogglesAndOpensTheSameMenuAsLongPress() {
        renderBrowse(
            state = loadedState(4).copy(viewerSource = ViewerSource.SEARCH, viewerIndex = 0),
            widthDp = 360,
            selectedTab = MobileTab.RESULTS,
        )

        composeRule.onNodeWithText("保存").assertExists()
        composeRule.onNodeWithContentDescription("更多").assertExists()
        composeRule.onNodeWithText("1 / 4").assertExists()

        // A single tap hides the chrome. Tap detection resolves after the double-tap timeout, so the
        // assertion is waited for instead of read immediately.
        composeRule.onNodeWithTag(ORIGINAL_VIEWER_IMAGE_TAG).performTouchInput { click() }
        composeRule.waitUntil(timeoutMillis = 5_000) { !toolbarVisible() }
        // Stay clear of the double-tap window before the next single tap.
        Thread.sleep(400)
        composeRule.onNodeWithTag(ORIGINAL_VIEWER_IMAGE_TAG).performTouchInput { click() }
        composeRule.waitUntil(timeoutMillis = 5_000) { toolbarVisible() }

        composeRule.onNodeWithContentDescription("更多").performClick()
        composeRule.onNodeWithTag(VIEWER_SHEET_TAG).assertExists()
        composeRule.onNodeWithText("旋转").assertExists()
        composeRule.onNode(isPopup(), useUnmergedTree = true).performTouchInput {
            click(Offset(center.x, center.y / 5f))
        }
        waitForViewerSheetToClose()

        composeRule.onNodeWithTag(ORIGINAL_VIEWER_IMAGE_TAG).performTouchInput { longClick() }
        composeRule.onNodeWithTag(VIEWER_SHEET_TAG).assertExists()
        pressBack()
        waitForViewerSheetToClose()
        composeRule.onNodeWithTag(ORIGINAL_VIEWER_IMAGE_TAG).assertExists()
    }

    @Test
    fun viewerToolbarDisablesSaveAndShareWhileTransferring() {
        renderBrowse(
            state = loadedState(2).copy(
                viewerSource = ViewerSource.SEARCH,
                viewerIndex = 0,
                transferAttempt = 1L,
                transferMessage = "正在保存 item-0.jpg",
                transferFraction = 0.5f,
            ),
            widthDp = 320,
            selectedTab = MobileTab.RESULTS,
        )

        composeRule.onNodeWithText("正在保存 item-0.jpg").assertExists()
        composeRule.onNodeWithText("取消").assertExists()
        composeRule.onNodeWithText("保存").assertIsNotEnabled()
        composeRule.onNodeWithText("分享").assertIsNotEnabled()
    }

    @Test
    fun viewerPagesAtOneTimesAndDoubleTapZoomBlocksPaging() {
        renderBrowse(
            state = loadedState(3).copy(viewerSource = ViewerSource.SEARCH, viewerIndex = 0),
            widthDp = 360,
            selectedTab = MobileTab.RESULTS,
        )

        composeRule.onNodeWithText("1 / 3").assertExists()
        currentViewerImage().performTouchInput { swipeLeft() }
        composeRule.waitUntil(timeoutMillis = 5_000) { stateHolder.value.viewerIndex == 1 }
        composeRule.onNodeWithText("2 / 3").assertExists()

        // Double tap zooms in, so the next horizontal swipe pans instead of turning the page.
        currentViewerImage().performTouchInput { doubleClick() }
        composeRule.waitForIdle()
        Thread.sleep(200)
        currentViewerImage().performTouchInput { swipeLeft() }
        composeRule.waitForIdle()
        assertEquals(1, stateHolder.value.viewerIndex)

        // Double tap again resets to 1x, so paging works again.
        currentViewerImage().performTouchInput { doubleClick() }
        composeRule.waitForIdle()
        Thread.sleep(200)
        currentViewerImage().performTouchInput { swipeLeft() }
        composeRule.waitUntil(timeoutMillis = 5_000) { stateHolder.value.viewerIndex == 2 }
        composeRule.onNodeWithText("3 / 3").assertExists()
    }

    @Test
    fun exportGalleryScreenshots() {
        stateHolder.value = loadedState(24)
        exportShot(widthDp = 320, heightDp = 640, fontScale = 1f, dark = false, name = "browse-results-light-320.png")
        exportShot(widthDp = 320, heightDp = 640, fontScale = 1f, dark = true, name = "browse-results-dark-320.png")
        exportShot(widthDp = 412, heightDp = 720, fontScale = 1.3f, dark = false, name = "browse-results-light-412-font13.png")
        exportShot(widthDp = 600, heightDp = 640, fontScale = 1f, dark = false, name = "browse-results-light-600.png")
        exportShot(widthDp = 840, heightDp = 640, fontScale = 1f, dark = true, name = "browse-results-dark-840.png")
    }

    @Test
    fun rotatedWideImageFillsTheFitHeightWithoutParentClamping() {
        val fixture = File(composeRule.activity.cacheDir, "wide-viewer-fixture.png")
        val bitmap = Bitmap.createBitmap(800, 200, Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(android.graphics.Color.CYAN)
        fixture.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
        bitmap.recycle()
        viewerImageUrl = android.net.Uri.fromFile(fixture).toString()
        renderBrowse(
            state = loadedState(1).copy(viewerSource = ViewerSource.SEARCH, viewerIndex = 0),
            widthDp = 320,
            heightDp = 640,
            selectedTab = MobileTab.RESULTS,
        )
        composeRule.waitUntil(timeoutMillis = 5_000) {
            composeRule.onAllNodesWithText("无法显示此原图").fetchSemanticsNodes().isEmpty() &&
                composeRule.onNodeWithContentDescription("item-0.jpg").fetchSemanticsNode().boundsInRoot.height < 200f
        }
        composeRule.onNodeWithContentDescription("更多").performClick()
        composeRule.onNodeWithText("旋转").performClick()
        pressBack()
        waitForViewerSheetToClose()
        val bounds = composeRule.onNodeWithContentDescription("item-0.jpg").fetchSemanticsNode().boundsInRoot
        assertEquals("旋转宽图的真实画布应完整适配640px高度", 640f, bounds.height, 2f)
        assertEquals(160f, bounds.width, 2f)
        val output = File(composeRule.activity.filesDir, "browse-viewer-wide-rotated.png")
        output.outputStream().use {
            composeRule.onNodeWithTag(SCREENSHOT_TAG).captureToImage().asAndroidBitmap()
                .compress(Bitmap.CompressFormat.PNG, 100, it)
        }
    }

    @Test
    fun viewerKeepsTheGridThumbnailWhileOnlyTheVisibleOriginalIsRequested() {
        val server = MockWebServer()
        val gates = CopyOnWriteArrayList<CountDownLatch>()
        val thumbnailPaths = CopyOnWriteArrayList<String>()
        val originalAttempts = ConcurrentHashMap<String, Int>()
        val thumbnailPng = solidPngBytes(800, 200, android.graphics.Color.RED)
        val originalPng = solidPngBytes(800, 200, android.graphics.Color.GREEN)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.requestUrl?.encodedPath ?: return MockResponse().setResponseCode(404)
                return when {
                    path.startsWith("/thumb/") -> {
                        thumbnailPaths += path
                        pngResponse(thumbnailPng)
                    }
                    path.startsWith("/original/") -> {
                        val attempt = (originalAttempts[path] ?: 0) + 1
                        originalAttempts[path] = attempt
                        when {
                            // The opened page fails once; the explicit retry of the same URL is
                            // gated so the placeholder can be inspected before it is released.
                            path == "/original/media-0" && attempt == 1 -> MockResponse().setResponseCode(500)
                            path == "/original/media-0" -> {
                                val gate = CountDownLatch(1)
                                gates += gate
                                if (gate.await(10, TimeUnit.SECONDS)) pngResponse(originalPng)
                                else MockResponse().setResponseCode(504)
                            }
                            else -> pngResponse(originalPng)
                        }
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        server.start()
        try {
            val base = server.url("/").toString().trimEnd('/')
            thumbnailUrlProvider = { item -> "$base/thumb/${item.mediaId}" }
            mediaUrlProvider = { item -> "$base/original/${item.mediaId}" }
            renderBrowse(state = loadedState(4), widthDp = 360, heightDp = 640, selectedTab = MobileTab.RESULTS)

            composeRule.waitUntil(timeoutMillis = 10_000) { thumbnailPaths.size >= 4 }
            composeRule.waitUntil(timeoutMillis = 5_000) {
                (0 until 4).all { index ->
                    val bitmap = composeRule.onNodeWithContentDescription("item-$index.jpg")
                        .captureToImage().asAndroidBitmap()
                    isThumbnailRed(bitmap.getPixel(bitmap.width / 4, bitmap.height / 2))
                }
            }
            composeRule.waitForIdle()
            assertTrue("网格阶段不得请求原图", originalAttempts.isEmpty())
            val thumbnailsAfterGrid = thumbnailPaths.size

            composeRule.onNodeWithContentDescription("item-0.jpg").performClick()
            composeRule.waitUntil(timeoutMillis = 10_000) { originalAttempts["/original/media-0"] == 1 }
            composeRule.waitUntil(timeoutMillis = 10_000) {
                composeRule.onAllNodesWithText("无法显示此原图").fetchSemanticsNodes().isNotEmpty()
            }
            composeRule.onNodeWithText("退出").assertExists()
            Thread.sleep(300)
            assertEquals("失败的原图不得重新下载缩略图：$thumbnailPaths", thumbnailsAfterGrid, thumbnailPaths.size)

            composeRule.onNodeWithText("重试").performClick()
            composeRule.waitUntil(timeoutMillis = 10_000) { (originalAttempts["/original/media-0"] ?: 0) >= 2 }
            assertTrue("原图被阻塞时应仍显示真实缩略图", awaitViewerPixel(::isThumbnailRed))
            assertEquals("阻塞期间不得重复取同图缩略图", thumbnailsAfterGrid, thumbnailPaths.size)
            composeRule.onNodeWithTag(ORIGINAL_VIEWER_IMAGE_TAG).performTouchInput { doubleClick() }
            assertFalse(
                "原图尺寸未知时应保持1倍居中，而不是放大占位图",
                awaitViewerPixel(::isThumbnailRed, sampleY = 0.35f),
            )

            gates.forEach { it.countDown() }
            composeRule.waitUntil(timeoutMillis = 10_000) {
                composeRule.onAllNodesWithText("无法显示此原图").fetchSemanticsNodes().isEmpty() &&
                    composeRule.onNodeWithContentDescription("item-0.jpg").fetchSemanticsNode().boundsInRoot.height < 200f
            }
            assertTrue("释放后应显示真实原图", awaitViewerPixel(::isOriginalGreen))
            assertTrue("原图请求不得超过当前及相邻两页", originalAttempts.keys.size <= 3)
            assertTrue("同 URL 显式重试后应成功", (originalAttempts["/original/media-0"] ?: 0) >= 2)
        } finally {
            gates.forEach { it.countDown() }
            runCatching { server.shutdown() }
        }
    }

    @Test
    fun exportRecommendationSearchAndViewerScreenshots() {
        stateHolder.value = recommendationState()
        selectedTabHolder.value = MobileTab.RECOMMENDATIONS
        exportShot(widthDp = 412, heightDp = 720, fontScale = 1.3f, dark = false, name = "browse-recommendations-light-412-font13.png")
        exportShot(widthDp = 840, heightDp = 640, fontScale = 1.5f, dark = true, name = "browse-recommendations-dark-840-font15.png")

        stateHolder.value = AppUiState(phase = ConnectionPhase.READY, queryImage = null)
        selectedTabHolder.value = MobileTab.SEARCH
        exportShot(widthDp = 360, heightDp = 480, fontScale = 1.5f, dark = false, name = "browse-search-light-360-font15.png")

        stateHolder.value = loadedState(3).copy(viewerSource = ViewerSource.SEARCH, viewerIndex = 0)
        selectedTabHolder.value = MobileTab.RESULTS
        exportShot(widthDp = 360, heightDp = 640, fontScale = 1f, dark = true, name = "browse-viewer-dark-360.png")
    }

    private fun selectDensity(density: GalleryDensity) {
        composeRule.onNodeWithTag(DENSITY_ENTRY_TAG).performClick()
        composeRule.onNodeWithTag(densityOptionTag(density)).performClick()
        composeRule.waitForIdle()
    }

    private fun toolbarVisible(): Boolean =
        composeRule.onAllNodesWithText("保存").fetchSemanticsNodes().isNotEmpty()

    private fun visibleColumnCount(): Int {
        composeRule.waitForIdle()
        val info = composeRule.runOnIdle { requireNotNull(gridStateRef).layoutInfo }
        return info.visibleItemsInfo.groupBy { it.offset.y }.values.maxOf { it.size }
    }

    private fun waitForViewerSheetToClose() {
        composeRule.waitUntil(timeoutMillis = 5_000) {
            composeRule.onAllNodesWithTag(VIEWER_SHEET_TAG).fetchSemanticsNodes().isEmpty()
        }
    }

    private fun currentViewerImage(): SemanticsNodeInteraction = composeRule.onNode(
        hasTestTag(ORIGINAL_VIEWER_IMAGE_TAG) and
            hasAnyDescendant(hasContentDescription(requireNotNull(stateHolder.value.currentViewerItem()).name)),
        useUnmergedTree = true,
    )

    /** Samples one pixel of the rendered page, deliberately off the centred loading indicator. */
    private fun awaitViewerPixel(matches: (Int) -> Boolean, sampleY: Float = 0.45f): Boolean {
        repeat(30) {
            composeRule.waitForIdle()
            val bitmap = composeRule.onNodeWithTag(SCREENSHOT_TAG).captureToImage().asAndroidBitmap()
            val pixel = bitmap.getPixel(bitmap.width / 2, (bitmap.height * sampleY).toInt())
            if (matches(pixel)) return true
            Thread.sleep(100)
        }
        return false
    }

    private fun isThumbnailRed(pixel: Int): Boolean =
        android.graphics.Color.red(pixel) > 180 &&
            android.graphics.Color.green(pixel) < 90 &&
            android.graphics.Color.blue(pixel) < 90

    private fun isOriginalGreen(pixel: Int): Boolean =
        android.graphics.Color.green(pixel) > 180 &&
            android.graphics.Color.red(pixel) < 90 &&
            android.graphics.Color.blue(pixel) < 90

    private fun pngResponse(bytes: ByteArray): MockResponse =
        MockResponse().setResponseCode(200)
            .setHeader("Content-Type", "image/png")
            .setBody(Buffer().write(bytes))

    private fun solidPngBytes(width: Int, height: Int, color: Int): ByteArray {
        val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(color)
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
        bitmap.recycle()
        return out.toByteArray()
    }

    private fun imeVisible(): Boolean {
        val insets = ViewCompat.getRootWindowInsets(composeRule.activity.window.decorView) ?: return false
        return insets.isVisible(WindowInsetsCompat.Type.ime())
    }

    private fun awaitImeVisible(): Boolean {
        repeat(30) {
            if (composeRule.runOnIdle { imeVisible() }) return true
            Thread.sleep(100)
        }
        return false
    }

    private fun exportShot(widthDp: Int, heightDp: Int, fontScale: Float, dark: Boolean, name: String) {
        renderBrowse(
            state = stateHolder.value,
            widthDp = widthDp,
            heightDp = heightDp,
            fontScale = fontScale,
            dark = dark,
        )
        val output = File(composeRule.activity.filesDir, name)
        output.outputStream().use { stream ->
            composeRule.onNodeWithTag(SCREENSHOT_TAG).captureToImage().asAndroidBitmap()
                .compress(Bitmap.CompressFormat.PNG, 100, stream)
        }
    }

    private fun renderBrowse(
        state: AppUiState,
        widthDp: Int,
        heightDp: Int = 640,
        density: Float = 1f,
        fontScale: Float = 1f,
        dark: Boolean = false,
        selectedTab: MobileTab? = null,
    ) {
        stateHolder.value = state
        if (selectedTab != null) selectedTabHolder.value = selectedTab
        shotConfig.value = BrowseShot(widthDp, heightDp, density, fontScale, dark)
        if (!contentSet) {
            contentSet = true
            composeRule.setContent { BrowseScreen() }
        }
        composeRule.waitForIdle()
    }

    @Composable
    private fun BrowseScreen() {
        val shot = shotConfig.value
        val base = LocalConfiguration.current
        val config = remember(base, shot.dark) {
            val night = if (shot.dark) Configuration.UI_MODE_NIGHT_YES else Configuration.UI_MODE_NIGHT_NO
            Configuration(base).apply {
                uiMode = (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or night
            }
        }
        CompositionLocalProvider(
            LocalConfiguration provides config,
            LocalDensity provides Density(shot.density, shot.fontScale),
        ) {
            ZvecTheme {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Surface(
                        modifier = Modifier
                            .requiredSize(shot.widthDp.dp, shot.heightDp.dp)
                            .testTag(SCREENSHOT_TAG),
                        color = MaterialTheme.colorScheme.background,
                    ) {
                        gridStateRef = rememberLazyGridState()
                        BrowseHarness(
                            selectedTab = selectedTabHolder.value,
                            gridState = requireNotNull(gridStateRef),
                        )
                    }
                }
            }
        }
    }

    @Composable
    private fun BrowseHarness(selectedTab: MobileTab, gridState: LazyGridState) {
        val state = stateHolder.value
        if (state.viewerIndex != null && state.viewerItems().isNotEmpty()) {
            OriginalViewer(
                state = state,
                mediaUrl = { item -> mediaUrlProvider(item) },
                thumbnailUrl = { item -> thumbnailUrlProvider(item) },
                onIndexChanged = { index -> stateHolder.value = stateHolder.value.copy(viewerIndex = index) },
                onClose = { stateHolder.value = stateHolder.value.copy(viewerSource = null, viewerIndex = null) },
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
                thumbnailUrl = { item -> thumbnailUrlProvider(item) },
                transferMessage = state.transferMessage,
                transferFraction = state.transferFraction,
                onCancelTransfer = {},
                onSelectedTab = { selectedTabHolder.value = it },
                onMode = {},
                onText = { text -> stateHolder.value = stateHolder.value.copy(searchText = text) },
                onTopK = { text -> stateHolder.value = stateHolder.value.copy(topKText = text) },
                onLibrary = {},
                onPickImage = {},
                onCancelUpload = {},
                onRetryUpload = {},
                onClearImage = {},
                onSearch = { searchCalls += 1 },
                onCancelSearch = {},
                onRetrySearch = {},
                onLoadMore = {},
                onRetryPage = {},
                onOpen = { index ->
                    stateHolder.value = stateHolder.value.copy(viewerSource = ViewerSource.SEARCH, viewerIndex = index)
                },
                onLoadRecommendations = {},
                onRecommendationsVisible = {},
                onOpenRecommendation = { index ->
                    stateHolder.value = stateHolder.value.copy(viewerSource = ViewerSource.RECOMMENDATIONS, viewerIndex = index)
                },
                onDisconnect = {},
            )
        }
    }
}

private data class BrowseShot(
    val widthDp: Int = 320,
    val heightDp: Int = 640,
    val density: Float = 1f,
    val fontScale: Float = 1f,
    val dark: Boolean = false,
)

private fun loadedState(count: Int): AppUiState = AppUiState(
    phase = ConnectionPhase.READY,
    searchText = "日落",
    activeSearchId = "search-1",
    searchPhase = SearchPhase.LOADED,
    results = (0 until count).map { index ->
        SearchItem(
            mediaId = "media-$index",
            name = "item-$index.jpg",
            width = 640,
            height = 480,
            libraryName = "图库",
        )
    }.toPersistentList(),
    totalResults = count,
    nextPage = 2,
)

private fun recommendationState(): AppUiState = AppUiState(
    phase = ConnectionPhase.READY,
    activeSearchId = "search-1",
    results = persistentListOf(),
    recommendations = RecommendationUiState(
        batchId = "batch-1",
        preloadedBatchId = "batch-1",
        items = (0 until 20).map { index ->
            RecommendationItem(
                itemId = "item-$index",
                mediaId = "media-$index",
                name = "recommended-$index.jpg",
                width = 640,
                height = 480,
                tags = emptyList(),
                libraryId = "library-1",
                libraryName = "图库",
                contentType = "image/jpeg",
                sizeBytes = 1024,
                bucket = "quality",
                thumbnailUrl = TEST_IMAGE_URL,
                previewUrl = TEST_IMAGE_URL,
            )
        }.toPersistentList(),
    ),
)

private const val SCREENSHOT_TAG = "browse-screenshot"
private const val TEST_IMAGE_URL = "android.resource://com.zvec.lanviewer/mipmap/ic_launcher"
