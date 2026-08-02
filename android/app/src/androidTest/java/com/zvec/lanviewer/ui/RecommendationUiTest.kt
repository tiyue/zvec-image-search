package com.zvec.lanviewer.ui

import android.graphics.Bitmap
import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.click
import androidx.compose.ui.test.isPopup
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.longClick
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToIndex
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.unit.dp
import androidx.test.espresso.Espresso.pressBack
import com.zvec.lanviewer.data.model.RecommendationAction
import com.zvec.lanviewer.data.model.RecommendationItem
import com.zvec.lanviewer.data.model.SearchItem
import kotlinx.collections.immutable.persistentListOf
import kotlinx.collections.immutable.toPersistentList
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import java.io.File

class RecommendationUiTest {
    @get:Rule
    val composeRule = createAndroidComposeRule<ComponentActivity>()

    @Test
    fun recommendationTabSurvivesOpeningViewerAndSystemBack() {
        composeRule.setContent {
            MaterialTheme(colorScheme = lightColorScheme()) {
                NavigationHarness()
            }
        }

        composeRule.onNodeWithText("推荐").performClick()
        composeRule.onNodeWithTag(RECOMMENDATION_GRID_TAG).performScrollToIndex(12)
        composeRule.onNodeWithContentDescription("recommended-12.jpg").performClick()
        composeRule.onNodeWithText("设备").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("recommended-12.jpg").assertExists()

        pressBack()

        composeRule.onNodeWithText("设备").assertExists()
        composeRule.onNodeWithContentDescription("recommended-12.jpg").assertExists()
    }

    @Test
    fun searchResultViewerStillReturnsToResults() {
        composeRule.setContent {
            MaterialTheme(colorScheme = lightColorScheme()) {
                NavigationHarness(initialViewerSource = ViewerSource.SEARCH)
            }
        }

        composeRule.onNodeWithText("设备").assertDoesNotExist()
        pressBack()

        composeRule.onNodeWithText("设备").assertExists()
        composeRule.onNodeWithContentDescription("search.jpg").assertExists()
    }

    @Test
    fun recommendationFeedbackClosesSheetBeforeSingleCallback() {
        val reactions = mutableListOf<Pair<String, RecommendationAction>>()
        setRecommendationViewer { itemId, action -> reactions += itemId to action }

        openViewerSheet()
        composeRule.onNodeWithText("喜欢").performClick()

        waitForSheetToClose()
        assertEquals(listOf("item-1" to RecommendationAction.LIKE), reactions)
    }

    @Test
    fun recommendationMenuShowsConfirmedLikeAndDoesNotResubmitIt() {
        val reactions = mutableListOf<Pair<String, RecommendationAction>>()
        setRecommendationViewer(reaction = RecommendationAction.LIKE) { itemId, action ->
            reactions += itemId to action
        }

        openViewerSheet()
        assertTrue(
            composeRule.onNodeWithText("已喜欢").fetchSemanticsNode()
                .config.contains(SemanticsProperties.Disabled),
        )
        composeRule.onNodeWithText("不喜欢").performClick()

        waitForSheetToClose()
        assertEquals(listOf("item-1" to RecommendationAction.DISLIKE), reactions)
    }

    @Test
    fun recommendationMenuShowsConfirmedDislikeAndDoesNotResubmitIt() {
        setRecommendationViewer(reaction = RecommendationAction.DISLIKE) { _, _ ->
            error("已确认的不喜欢不得重复提交")
        }

        openViewerSheet()
        assertTrue(
            composeRule.onNodeWithText("已不喜欢").fetchSemanticsNode()
                .config.contains(SemanticsProperties.Disabled),
        )
        composeRule.onNodeWithText("喜欢").assertExists()
    }

    @Test
    fun searchViewerHasNoRecommendationFeedbackAndKeepsOtherActions() {
        var shareCount = 0
        composeRule.setContent {
            MaterialTheme(colorScheme = lightColorScheme()) {
                OriginalViewer(
                    state = searchViewerState(),
                    mediaUrl = { TEST_IMAGE_URL },
                    onIndexChanged = {},
                    onLoadMore = {},
                    onClose = {},
                    onSave = {},
                    onShare = { shareCount += 1 },
                    onReaction = { _, _ -> error("搜索查看器不得提交推荐反馈") },
                )
            }
        }

        openViewerSheet()
        composeRule.onNodeWithText("喜欢").assertDoesNotExist()
        composeRule.onNodeWithText("不喜欢").assertDoesNotExist()
        composeRule.onNodeWithText("旋转").performClick()
        composeRule.onNodeWithText("详细信息").performClick()
        composeRule.onNodeWithText("文件名：search.jpg").assertExists()
        composeRule.onNodeWithText("分享").performClick()

        waitForSheetToClose()
        assertEquals(1, shareCount)
    }

    @Test
    fun recommendationSheetDismissesOnOutsideTapAndBack() {
        setRecommendationViewer { _, _ -> }
        openViewerSheet()

        composeRule.onNode(isPopup(), useUnmergedTree = true).performTouchInput {
            click(Offset(center.x, center.y / 2f))
        }
        waitForSheetToClose()

        openViewerSheet()
        pressBack()
        waitForSheetToClose()
        composeRule.onNodeWithContentDescription("recommended.jpg").assertExists()
    }

    @Test
    fun recommendationCardsHaveNoFeedbackControlsInLightNarrowLayout() {
        renderNarrowRecommendationPanel(dark = false, screenshotName = "recommendations-light-320.png")
    }

    @Test
    fun recommendationCardsHaveNoFeedbackControlsInDarkNarrowLayout() {
        renderNarrowRecommendationPanel(dark = true, screenshotName = "recommendations-dark-320.png")
    }

    private fun setRecommendationViewer(
        reaction: RecommendationAction? = null,
        onReaction: (String, RecommendationAction) -> Unit,
    ) {
        composeRule.setContent {
            MaterialTheme(colorScheme = lightColorScheme()) {
                Box(Modifier.fillMaxSize()) {
                    OriginalViewer(
                        state = recommendationViewerState(reaction),
                        mediaUrl = { TEST_IMAGE_URL },
                        onIndexChanged = {},
                        onLoadMore = {},
                        onClose = {},
                        onSave = {},
                        onShare = {},
                        onReaction = onReaction,
                    )
                }
            }
        }
    }

    private fun openViewerSheet() {
        composeRule.onNodeWithTag(ORIGINAL_VIEWER_IMAGE_TAG).performTouchInput { longClick() }
        composeRule.onNodeWithText("保存").assertExists()
    }

    private fun waitForSheetToClose() {
        composeRule.waitUntil(timeoutMillis = 5_000) {
            composeRule.onAllNodesWithText("保存").fetchSemanticsNodes().isEmpty()
        }
    }

    private fun renderNarrowRecommendationPanel(dark: Boolean, screenshotName: String) {
        composeRule.setContent {
            MaterialTheme(colorScheme = if (dark) testDarkColors else testLightColors) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Surface(
                        modifier = Modifier.width(320.dp).fillMaxHeight().testTag(PANEL_TAG),
                        color = MaterialTheme.colorScheme.background,
                    ) {
                        RecommendationPanel(
                            state = readyState(),
                            onLoad = {},
                            onVisible = {},
                            onOpen = {},
                            gridState = rememberLazyGridState(),
                        )
                    }
                }
            }
        }

        listOf("喜欢", "不喜欢", "已喜欢", "已不喜欢", "已跳过").forEach { label ->
            composeRule.onNodeWithText(label).assertDoesNotExist()
        }
        composeRule.onNodeWithContentDescription("recommended.jpg").assertExists()
        composeRule.waitForIdle()
        val output = File(composeRule.activity.filesDir, screenshotName)
        output.outputStream().use { stream ->
            composeRule.onNodeWithTag(PANEL_TAG).captureToImage().asAndroidBitmap()
                .compress(Bitmap.CompressFormat.PNG, 100, stream)
        }
    }
}

@Composable
private fun NavigationHarness(initialViewerSource: ViewerSource? = null) {
    var selectedTab by rememberSaveable { mutableStateOf(MobileTab.RESULTS) }
    var viewerSource by remember { mutableStateOf(initialViewerSource) }
    var viewerIndex by remember { mutableStateOf(0) }
    val recommendationGridState = rememberLazyGridState()
    val state = readyState().copy(
        viewerSource = viewerSource,
        viewerIndex = if (viewerSource == null) null else viewerIndex,
    )
    if (viewerSource != null) {
        OriginalViewer(
            state = state,
            mediaUrl = { TEST_IMAGE_URL },
            onIndexChanged = {},
            onLoadMore = {},
            onClose = { viewerSource = null },
            onSave = {},
            onShare = {},
            onReaction = { _, _ -> },
        )
    } else {
        SearchScreen(
            state = state,
            selectedTab = selectedTab,
            recommendationGridState = recommendationGridState,
            modifier = Modifier.fillMaxSize(),
            mediaUrl = { TEST_IMAGE_URL },
            onSelectedTab = { selectedTab = it },
            onMode = {},
            onText = {},
            onTopK = {},
            onLibrary = {},
            onPickImage = {},
            onCancelUpload = {},
            onClearImage = {},
            onSearch = {},
            onCancelSearch = {},
            onLoadMore = {},
            onOpen = { index ->
                viewerIndex = index
                viewerSource = ViewerSource.SEARCH
            },
            onLoadRecommendations = {},
            onRecommendationsVisible = {},
            onOpenRecommendation = { index ->
                viewerIndex = index
                viewerSource = ViewerSource.RECOMMENDATIONS
            },
            onDisconnect = {},
        )
    }
}

private fun readyState(): AppUiState = AppUiState(
    phase = ConnectionPhase.READY,
    activeSearchId = "search-1",
    results = persistentListOf(searchItem()),
    recommendations = RecommendationUiState(
        batchId = "batch-1",
        preloadedBatchId = "batch-1",
        items = (0 until 20).map(::recommendationItem).toPersistentList(),
    ),
)

private fun recommendationViewerState(reaction: RecommendationAction? = null): AppUiState {
    val state = readyState()
    return state.copy(
        recommendations = state.recommendations.copy(
            reactions = reaction?.let { mapOf("item-1" to it) }.orEmpty(),
        ),
        viewerSource = ViewerSource.RECOMMENDATIONS,
        viewerIndex = 0,
    )
}

private fun searchViewerState(): AppUiState = readyState().copy(
    viewerSource = ViewerSource.SEARCH,
    viewerIndex = 0,
)

private fun recommendationItem(index: Int = 0): RecommendationItem = RecommendationItem(
    itemId = "item-${index + 1}",
    mediaId = "media-${index + 1}",
    name = if (index == 0) "recommended.jpg" else "recommended-$index.jpg",
    width = 640,
    height = 480,
    tags = listOf("风景"),
    libraryId = "library-1",
    libraryName = "图库",
    contentType = "image/jpeg",
    sizeBytes = 1024,
    bucket = "quality",
    thumbnailUrl = TEST_IMAGE_URL,
    previewUrl = TEST_IMAGE_URL,
)

private fun searchItem(): SearchItem = SearchItem(
    mediaId = "search-media-1",
    name = "search.jpg",
    width = 640,
    height = 480,
    tags = emptyList(),
    libraryId = "library-1",
    libraryName = "图库",
    contentType = "image/jpeg",
    sizeBytes = 1024,
)

private const val TEST_IMAGE_URL = "android.resource://com.zvec.lanviewer/mipmap/ic_launcher"
private const val PANEL_TAG = "recommendation-panel"

private val testLightColors = lightColorScheme(
    primary = Color(0xFF171717),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFECECEC),
    onPrimaryContainer = Color(0xFF171717),
    secondary = Color(0xFF777777),
    background = Color(0xFFF7F7F8),
    surface = Color.White,
    surfaceVariant = Color(0xFFECEFF3),
    error = Color(0xFFB91C1C),
)

private val testDarkColors = darkColorScheme(
    primary = Color(0xFFF1F1F1),
    onPrimary = Color(0xFF171717),
    primaryContainer = Color(0xFF303030),
    onPrimaryContainer = Color(0xFFF1F1F1),
    secondary = Color(0xFFB6B6B6),
    background = Color(0xFF121212),
    surface = Color(0xFF1C1C1C),
    surfaceVariant = Color(0xFF2B2D30),
    error = Color(0xFFFFB4AB),
)
