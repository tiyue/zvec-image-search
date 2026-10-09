package com.zvec.lanviewer.ui

import android.content.Context
import android.content.ContextWrapper
import android.content.SharedPreferences
import android.content.res.Configuration
import android.graphics.Bitmap
import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.requiredSize
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onFirst
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeLeft
import androidx.compose.ui.unit.dp
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.test.platform.app.InstrumentationRegistry
import com.zvec.lanviewer.data.discovery.UdpDiscoveryService
import com.zvec.lanviewer.data.local.ConnectionMode
import com.zvec.lanviewer.data.local.ConnectionStore
import com.zvec.lanviewer.data.local.InstallationStore
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.local.SavedFilesStore
import com.zvec.lanviewer.data.network.LanApiClient
import com.zvec.lanviewer.data.network.ResumableDownloader
import com.zvec.lanviewer.data.repository.ZvecRepository
import com.zvec.lanviewer.data.security.InMemoryTokenStore
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Rule
import org.junit.Test

/**
 * R4 原生验收：真实 [AppViewModel] 提供 remote 目标，真实 [OriginalViewer] / [SearchScreen] 渲染，
 * MockWebServer 返回真实 PNG。断言网格/预载/查看器始终使用当前 origin 的
 * `/remote-thumbnail` 与按需 `/preview`、显式 `/original`，并导出 remote-* 截图供协调者目检。
 */
class RemoteBrowseUiTest {
    @get:Rule
    val composeRule = createAndroidComposeRule<ComponentActivity>()

    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val targetContext get() = instrumentation.targetContext

    private val servers = CopyOnWriteArrayList<MockWebServer>()
    private val stores = CopyOnWriteArrayList<ViewModelStore>()
    private val paths = CopyOnWriteArrayList<String>()
    private lateinit var mediaIds: List<String>

    @After
    fun tearDown() {
        stores.forEach { it.clear() }
        stores.clear()
        servers.forEach { server -> runCatching { server.shutdown() } }
        servers.clear()
    }

    @Test
    fun remoteGridUsesRemoteThumbnailRouteForEveryGridRequest() {
        val vm = startRemoteViewModel(mediaProfiles = listOf("remote-v1"))
        composeRule.setContent { RemoteGridContent(vm) }

        composeRule.waitUntil(timeoutMillis = 10_000) { mediaIds.all { countMedia(it, "remote-thumbnail") >= 1 } }

        assertEquals("REMOTE 网格不得请求 LAN 缩略图", 0, countSuffix("/thumbnail"))
        assertEquals(0, countSuffix("/preview"))
        assertEquals(0, countSuffix("/original"))
    }

    @Test
    fun remoteViewerRequestsOnlyCurrentPreviewAndOriginalOnExplicitAction() {
        val vm = startRemoteViewModel(mediaProfiles = listOf("remote-v1"))
        onMain { vm.openViewer(0) }
        composeRule.setContent { RemoteViewerContent(vm) }

        composeRule.waitUntil(timeoutMillis = 10_000) { countMedia(mediaIds[0], "preview") >= 1 }
        composeRule.waitUntil(timeoutMillis = 10_000) { countMedia(mediaIds[0], "remote-thumbnail") >= 1 }
        Thread.sleep(400)
        assertEquals("相邻页不得请求 preview", 0, countMedia(mediaIds[1], "preview"))
        assertEquals("显式查看前不得请求 original", 0, countSuffix("/original"))

        composeRule.onAllNodesWithTag(ORIGINAL_VIEWER_IMAGE_TAG).onFirst().performTouchInput { swipeLeft() }
        composeRule.waitUntil(timeoutMillis = 10_000) { vm.state.value.viewerIndex == 1 }
        composeRule.waitUntil(timeoutMillis = 10_000) { countMedia(mediaIds[1], "preview") >= 1 }
        Thread.sleep(300)
        assertEquals("滑页后仍不得有 original", 0, countSuffix("/original"))
        assertEquals("当前页 preview 只发一次", 1, countMedia(mediaIds[1], "preview"))
        assertEquals("相邻页不得请求 preview", 0, countMedia(mediaIds[2], "preview"))

        composeRule.onNodeWithText("查看原图", substring = true).performClick()
        composeRule.waitUntil(timeoutMillis = 10_000) { countMedia(mediaIds[1], "original") >= 1 }
        assertEquals("显式查看只加载当前页原图", 1, countSuffix("/original"))
    }

    @Test
    fun oldDesktopWithoutRemotePreviewShowsUpgradeAndAvoidsLargeFiles() {
        val vm = startRemoteViewModel(mediaProfiles = emptyList())
        assertFalse(vm.state.value.supportsRemoteMedia)
        onMain { vm.openViewer(0) }
        composeRule.setContent { RemoteViewerContent(vm) }

        composeRule.onNodeWithText("电脑端未提供远程预览", substring = true).assertExists()
        composeRule.onNodeWithText("查看原图", substring = true).assertExists()
        Thread.sleep(600)
        assertEquals("无 remote-v1 时不得请求 preview", 0, countSuffix("/preview"))
        assertEquals("无 remote-v1 时不得自动拉原图", 0, countSuffix("/original"))

        composeRule.onNodeWithText("查看原图", substring = true).performClick()
        composeRule.waitUntil(timeoutMillis = 10_000) { countMedia(mediaIds[0], "original") >= 1 }
        assertEquals(1, countSuffix("/original"))
    }

    @Test
    fun exportRemoteViewerScreenshots() {
        val vm = startRemoteViewModel(mediaProfiles = listOf("remote-v1"))
        onMain { vm.openViewer(0) }
        val shot = mutableStateOf(RemoteShot(widthDp = 360, heightDp = 640, dark = false))
        composeRule.setContent { RemoteScreenshotContent(vm, shot) }

        composeRule.waitUntil(timeoutMillis = 10_000) { countMedia(mediaIds[0], "preview") >= 1 }
        capture("remote-viewer-preview-light-360.png")
        composeRule.runOnIdle { shot.value = shot.value.copy(dark = true) }
        capture("remote-viewer-preview-dark-360.png")

        composeRule.runOnIdle { shot.value = shot.value.copy(dark = false) }
        composeRule.onAllNodesWithTag(ORIGINAL_VIEWER_IMAGE_TAG).onFirst().performTouchInput { swipeLeft() }
        composeRule.waitUntil(timeoutMillis = 10_000) { vm.state.value.viewerIndex == 1 }
        capture("remote-viewer-page2-light-360.png")

        onMain { vm.requestViewerOriginal() }
        composeRule.waitUntil(timeoutMillis = 10_000) { countSuffix("/original") >= 1 }
        composeRule.runOnIdle { shot.value = shot.value.copy(dark = true) }
        capture("remote-viewer-original-dark-360.png")
    }

    @Test
    fun exportSearchDeviceScreenshots() {
        val vm = startRemoteViewModel(mediaProfiles = listOf("remote-v1"))
        val dark = mutableStateOf(false)
        composeRule.setContent { SearchDeviceScreenshotContent(vm, dark) }
        composeRule.waitForIdle()

        // The device page shows the real registered remote endpoint, not a placeholder.
        composeRule.onNodeWithText("连接设备").assertExists()
        composeRule.onNodeWithText("当前连接 · 远程", substring = true).assertExists()
        capture("remote-search-device-light-360.png")

        composeRule.runOnIdle { dark.value = true }
        capture("remote-search-device-dark-360.png")
    }

    @Test
    fun exportRemoteDiscoveryScreenshotAndAssertRemoteEntryOnly() {
        val vm = startRemoteDiscoveryViewModel()
        val dark = mutableStateOf(false)
        composeRule.setContent { AppScreenshotContent(vm, dark) }
        composeRule.waitForIdle()

        composeRule.onNodeWithText("非局域网（远程）").assertExists()
        composeRule.onNodeWithText("连接远程").assertExists()
        // The REMOTE entry must not show the LAN-only HTTP warning or automatic-discovery controls.
        composeRule.onNodeWithText("家庭专用网络", substring = true).assertDoesNotExist()
        composeRule.onNodeWithText("自动发现", substring = true).assertDoesNotExist()
        composeRule.onNodeWithText("重新搜索", substring = true).assertDoesNotExist()
        composeRule.onNodeWithText("FRPC", substring = true).assertExists()
        capture("remote-discovery-remote-light-360.png")
    }

    // --- 压缩布局/测试基建 ------------------------------------------------------

    @Composable
    private fun RemoteViewerContent(vm: AppViewModel) {
        val state by vm.state.collectAsState()
        ZvecTheme {
            OriginalViewer(
                state = state,
                mediaUrl = vm::mediaUrl,
                thumbnailUrl = { vm.thumbnailUrl(it) },
                viewerTarget = vm::viewerTarget,
                onViewOriginal = vm::requestViewerOriginal,
                onIndexChanged = vm::setViewerIndex,
                onClose = {},
                onSave = {},
                onShare = {},
                onCancelTransfer = {},
                onReaction = { _, _ -> },
            )
        }
    }

    @Composable
    private fun RemoteGridContent(vm: AppViewModel) {
        val state by vm.state.collectAsState()
        ZvecTheme {
            SearchScreen(
                state = state,
                selectedTab = MobileTab.RESULTS,
                searchGridState = rememberLazyGridState(),
                recommendationGridState = rememberLazyGridState(),
                modifier = Modifier.fillMaxSize(),
                thumbnailUrl = vm::thumbnailUrl,
                transferMessage = state.transferMessage,
                transferFraction = state.transferFraction,
                onCancelTransfer = {},
                onSelectedTab = {},
                onMode = {},
                onText = {},
                onTopK = {},
                onLibrary = {},
                onPickImage = {},
                onCancelUpload = {},
                onRetryUpload = {},
                onClearImage = {},
                onSearch = {},
                onCancelSearch = {},
                onRetrySearch = {},
                onLoadMore = {},
                onRetryPage = {},
                onOpen = {},
                onLoadRecommendations = {},
                onRecommendationsVisible = {},
                onOpenRecommendation = {},
                onDisconnect = {},
            )
        }
    }

    @Composable
    private fun RemoteScreenshotContent(vm: AppViewModel, shot: androidx.compose.runtime.MutableState<RemoteShot>) {
        val state by vm.state.collectAsState()
        val config = shot.value
        val base = LocalConfiguration.current
        val night = if (config.dark) Configuration.UI_MODE_NIGHT_YES else Configuration.UI_MODE_NIGHT_NO
        val cfg = remember(base, config.dark) {
            Configuration(base).apply {
                uiMode = (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or night
            }
        }
        CompositionLocalProvider(LocalConfiguration provides cfg) {
            ZvecTheme {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Surface(
                        modifier = Modifier
                            .requiredSize(config.widthDp.dp, config.heightDp.dp)
                            .testTag(SCREENSHOT_TAG),
                        color = MaterialTheme.colorScheme.background,
                    ) {
                        OriginalViewer(
                            state = state,
                            mediaUrl = vm::mediaUrl,
                            thumbnailUrl = { vm.thumbnailUrl(it) },
                            viewerTarget = vm::viewerTarget,
                            onViewOriginal = vm::requestViewerOriginal,
                            onIndexChanged = vm::setViewerIndex,
                            onClose = {},
                            onSave = {},
                            onShare = {},
                            onCancelTransfer = {},
                            onReaction = { _, _ -> },
                        )
                    }
                }
            }
        }
    }

    /** Phone light/dark capture of the production device page with the real registered endpoints. */
    @Composable
    private fun SearchDeviceScreenshotContent(
        vm: AppViewModel,
        dark: androidx.compose.runtime.MutableState<Boolean>,
    ) {
        val state by vm.state.collectAsState()
        val base = LocalConfiguration.current
        val night = if (dark.value) Configuration.UI_MODE_NIGHT_YES else Configuration.UI_MODE_NIGHT_NO
        val cfg = remember(base, dark.value) {
            Configuration(base).apply {
                uiMode = (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or night
            }
        }
        CompositionLocalProvider(LocalConfiguration provides cfg) {
            ZvecTheme {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Surface(
                        modifier = Modifier.requiredSize(360.dp, 640.dp).testTag(SCREENSHOT_TAG),
                        color = MaterialTheme.colorScheme.background,
                    ) {
                        SearchScreen(
                            state = state,
                            selectedTab = MobileTab.DEVICE,
                            searchGridState = rememberLazyGridState(),
                            recommendationGridState = rememberLazyGridState(),
                            modifier = Modifier.fillMaxSize(),
                            thumbnailUrl = vm::thumbnailUrl,
                            transferMessage = state.transferMessage,
                            transferFraction = state.transferFraction,
                            onCancelTransfer = {},
                            onSelectedTab = {},
                            onMode = {},
                            onText = {},
                            onTopK = {},
                            onLibrary = {},
                            onPickImage = {},
                            onCancelUpload = {},
                            onRetryUpload = {},
                            onClearImage = {},
                            onSearch = {},
                            onCancelSearch = {},
                            onRetrySearch = {},
                            onLoadMore = {},
                            onRetryPage = {},
                            onOpen = {},
                            onLoadRecommendations = {},
                            onRecommendationsVisible = {},
                            onOpenRecommendation = {},
                            onDisconnect = {},
                        )
                    }
                }
            }
        }
    }

    /** Full production [ZvecApp] capture, used to inspect the REMOTE discovery/manual entry. */
    @Composable
    private fun AppScreenshotContent(
        vm: AppViewModel,
        dark: androidx.compose.runtime.MutableState<Boolean>,
    ) {
        val base = LocalConfiguration.current
        val night = if (dark.value) Configuration.UI_MODE_NIGHT_YES else Configuration.UI_MODE_NIGHT_NO
        val cfg = remember(base, dark.value) {
            Configuration(base).apply {
                uiMode = (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or night
            }
        }
        CompositionLocalProvider(LocalConfiguration provides cfg) {
            ZvecTheme {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Surface(
                        modifier = Modifier.requiredSize(360.dp, 640.dp).testTag(SCREENSHOT_TAG),
                        color = MaterialTheme.colorScheme.background,
                    ) {
                        ZvecApp(vm)
                    }
                }
            }
        }
    }

    private fun startRemoteViewModel(mediaProfiles: List<String>): AppViewModel {
        val runId = UUID.randomUUID().toString().take(8)
        mediaIds = (0 until ITEM_COUNT).map { "$runId-m$it" }
        val server = MockWebServer().apply { start() }
        servers += server
        val origin = "http://127.0.0.1:${server.port}"
        server.dispatcher = mediaDispatcher(origin, mediaProfiles)

        val context = IsolatedContext(targetContext)
        val connectionStore = ConnectionStore(context)
        connectionStore.write(
            SavedConnection(
                baseUrl = origin,
                instanceId = INSTANCE_ID,
                displayName = "测试电脑",
                mode = ConnectionMode.REMOTE,
                lanBaseUrl = origin,
                remoteBaseUrl = origin,
                supportsRemoteMedia = mediaProfiles.contains("remote-v1"),
            ),
        )
        val tokenStore = InMemoryTokenStore(SYNTHETIC_TOKEN)
        val api = LanApiClient(connectionStore, tokenStore, LanApiClient.DEFAULT_CONCURRENCY)
        val repository = ZvecRepository(
            context = context,
            discovery = UdpDiscoveryService(),
            connectionStore = connectionStore,
            installationStore = InstallationStore(context),
            tokenStore = tokenStore,
            api = api,
            downloader = ResumableDownloader(api.httpClient),
        )
        val store = ViewModelStore()
        stores += store
        val vm = createViewModel(store, context, repository, SavedFilesStore(context))
        awaitState(vm, "远程连接就绪") { it.phase == ConnectionPhase.READY }
        onMain { vm.setSearchText("cat") }
        onMain { vm.search() }
        awaitState(vm, "远程搜索结果") { it.results.size == ITEM_COUNT }
        return vm
    }

    /**
     * A REMOTE entry with no token restores into the discovery phase. It must show the REMOTE
     * manual/pairing form without launching LAN UDP discovery, so no server is started here.
     */
    private fun startRemoteDiscoveryViewModel(): AppViewModel {
        val context = IsolatedContext(targetContext)
        val connectionStore = ConnectionStore(context)
        connectionStore.write(
            SavedConnection(
                baseUrl = "http://203.0.113.7:38522",
                instanceId = INSTANCE_ID,
                displayName = "测试电脑",
                mode = ConnectionMode.REMOTE,
                lanBaseUrl = null,
                remoteBaseUrl = "http://203.0.113.7:38522",
                supportsRemoteMedia = true,
            ),
        )
        val tokenStore = InMemoryTokenStore(null)
        val api = LanApiClient(connectionStore, tokenStore, LanApiClient.DEFAULT_CONCURRENCY)
        val repository = ZvecRepository(
            context = context,
            discovery = UdpDiscoveryService(),
            connectionStore = connectionStore,
            installationStore = InstallationStore(context),
            tokenStore = tokenStore,
            api = api,
            downloader = ResumableDownloader(api.httpClient),
        )
        val store = ViewModelStore()
        stores += store
        val vm = createViewModel(store, context, repository, SavedFilesStore(context))
        awaitState(vm, "远程发现入口就绪") {
            it.phase == ConnectionPhase.DISCOVERY && it.discoveryMode == ConnectionMode.REMOTE
        }
        return vm
    }

    private fun mediaDispatcher(origin: String, mediaProfiles: List<String>): Dispatcher = object : Dispatcher() {
        override fun dispatch(request: RecordedRequest): MockResponse {
            val path = request.requestUrl?.encodedPath ?: return MockResponse().setResponseCode(404)
            paths += path
            return when {
                request.method == "GET" && path == "/api/v1/status" -> jsonResponse(200, statusJson(origin, mediaProfiles))
                request.method == "GET" && path == "/api/v1/libraries" -> jsonResponse(200, LIBRARIES_JSON)
                request.method == "POST" && path == "/api/v1/searches" -> jsonResponse(201, """{"search_id":"$SEARCH_ID"}""")
                request.method == "GET" && path == "/api/v1/searches/$SEARCH_ID" -> jsonResponse(200, pageBody())
                request.method == "DELETE" -> jsonResponse(200, "{}")
                path.endsWith("/remote-thumbnail") -> pngResponse(thumbnailPng)
                path.endsWith("/preview") -> pngResponse(previewPng)
                path.endsWith("/original") -> pngResponse(originalPng)
                else -> MockResponse().setResponseCode(404)
            }
        }
    }

    private fun statusJson(origin: String, mediaProfiles: List<String>): String {
        val profiles = mediaProfiles.joinToString(",") { "\"$it\"" }
        return """{"protocol":1,"instance_id":"$INSTANCE_ID","name":"测试电脑",""" +
            """"connection_endpoints":{"lan":"$origin","remote":"$origin"},"media_profiles":[$profiles]}"""
    }

    private fun pageBody(): String {
        val items = mediaIds.mapIndexed { index, mediaId ->
            """{"media_id":"$mediaId","name":"item-$index.jpg","width":640,"height":480,"tags":[],"library_id":"lib-1","library_name":"图库一","content_type":"image/jpeg","size_bytes":1024}"""
        }.joinToString(",")
        return """{"search_id":"$SEARCH_ID","page":1,"page_size":100,"total":${mediaIds.size},"items":[$items]}"""
    }

    private fun countSuffix(suffix: String): Int = paths.count { it.endsWith(suffix) }

    private fun countMedia(mediaId: String, suffix: String): Int =
        paths.count { it == "/api/v1/media/$mediaId/$suffix" }

    private fun capture(name: String) {
        if (name.startsWith("remote-viewer-")) {
            val expected = if (name.contains("original")) android.graphics.Color.GREEN
                else android.graphics.Color.BLUE
            composeRule.waitUntil(timeoutMillis = 10_000) {
                val bitmap = composeRule.onNodeWithTag(SCREENSHOT_TAG)
                    .captureToImage().asAndroidBitmap()
                val ready = bitmap.getPixel(bitmap.width / 2, bitmap.height / 2) == expected
                bitmap.recycle()
                ready
            }
        }
        composeRule.waitForIdle()
        val output = File(targetContext.filesDir, name)
        output.outputStream().use { stream ->
            composeRule.onNodeWithTag(SCREENSHOT_TAG).captureToImage().asAndroidBitmap()
                .compress(Bitmap.CompressFormat.PNG, 100, stream)
        }
    }

    private fun createViewModel(
        store: ViewModelStore,
        context: Context,
        repository: ZvecRepository,
        savedFilesStore: SavedFilesStore,
    ): AppViewModel {
        val factory = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T {
                require(modelClass.isAssignableFrom(AppViewModel::class.java)) { "意外的 ViewModel 类型" }
                return AppViewModel(context, repository, savedFilesStore, SavedStateHandle()) as T
            }
        }
        var created: AppViewModel? = null
        onMain { created = ViewModelProvider(store, factory)[AppViewModel::class.java] }
        return requireNotNull(created)
    }

    private class IsolatedContext(base: Context) : ContextWrapper(base) {
        private val prefix = "zvec-remote-browse-${UUID.randomUUID()}"
        private val isolatedCacheDir = File(base.cacheDir, prefix).apply { mkdirs() }
        private val isolatedFilesDir = File(base.filesDir, prefix).apply { mkdirs() }

        override fun getApplicationContext(): Context = this

        override fun getSharedPreferences(name: String, mode: Int): SharedPreferences =
            super.getSharedPreferences("$prefix-$name", mode)

        override fun getCacheDir(): File = isolatedCacheDir

        override fun getFilesDir(): File = isolatedFilesDir
    }

    private fun onMain(block: () -> Unit) {
        instrumentation.runOnMainSync { block() }
    }

    private fun awaitState(
        vm: AppViewModel,
        description: String,
        timeoutMillis: Long = 10_000,
        predicate: (AppUiState) -> Boolean,
    ): AppUiState {
        var last = vm.state.value
        val deadline = android.os.SystemClock.uptimeMillis() + timeoutMillis
        while (android.os.SystemClock.uptimeMillis() < deadline) {
            last = vm.state.value
            if (predicate(last)) return last
            Thread.sleep(20)
        }
        throw AssertionError("等待状态超时：$description；当前状态=$last")
    }

    private fun solidPngBytes(width: Int, height: Int, color: Int): ByteArray {
        val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(color)
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
        bitmap.recycle()
        return out.toByteArray()
    }

    private val thumbnailPng: ByteArray by lazy { solidPngBytes(320, 200, android.graphics.Color.RED) }
    private val previewPng: ByteArray by lazy { solidPngBytes(320, 200, android.graphics.Color.BLUE) }
    private val originalPng: ByteArray by lazy { solidPngBytes(320, 200, android.graphics.Color.GREEN) }

    private fun pngResponse(bytes: ByteArray): MockResponse =
        MockResponse().setResponseCode(200).setHeader("Content-Type", "image/png").setBody(Buffer().write(bytes))

    private fun jsonResponse(code: Int, body: String): MockResponse =
        MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(body)

    private data class RemoteShot(val widthDp: Int, val heightDp: Int, val dark: Boolean)

    private companion object {
        const val INSTANCE_ID = "inst-remote-browse"
        const val SYNTHETIC_TOKEN = "synthetic-remote-browse-token"
        const val SEARCH_ID = "search-remote"
        const val ITEM_COUNT = 3
        const val SCREENSHOT_TAG = "remote-screenshot"
        const val LIBRARIES_JSON =
            """{"libraries":[{"id":"lib-1","name":"图库一","count":3},{"id":"lib-2","name":"图库二","count":1}]}"""
    }
}
