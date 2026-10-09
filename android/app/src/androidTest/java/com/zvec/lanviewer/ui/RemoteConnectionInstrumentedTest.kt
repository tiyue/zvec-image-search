package com.zvec.lanviewer.ui

import android.content.Context
import android.content.ContextWrapper
import android.content.SharedPreferences
import android.net.Uri
import android.os.SystemClock
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.zvec.lanviewer.data.discovery.UdpDiscoveryService
import com.zvec.lanviewer.data.local.ConnectionMode
import com.zvec.lanviewer.data.local.ConnectionStore
import com.zvec.lanviewer.data.local.InstallationStore
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.local.SavedFilesStore
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.StatusResponse
import com.zvec.lanviewer.data.network.LanApiClient
import com.zvec.lanviewer.data.network.ResumableDownloader
import com.zvec.lanviewer.data.repository.ZvecRepository
import com.zvec.lanviewer.data.security.InMemoryTokenStore
import java.io.File
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.onSubscription
import kotlinx.coroutines.launch
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * R4 真实调用链验收：两个 MockWebServer 入口，同一 instance_id，Bearer 认证的 /status 携带双方入口。
 * 全部偏好/文件/缓存都在隔离目录，绝不触碰用户连接、Keystore 或真实 FRPC。
 */
@RunWith(AndroidJUnit4::class)
class RemoteConnectionInstrumentedTest {
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val targetContext get() = instrumentation.targetContext

    private val servers = CopyOnWriteArrayList<MockWebServer>()
    private val viewModelStores = CopyOnWriteArrayList<ViewModelStore>()
    private val openGates = CopyOnWriteArrayList<CountDownLatch>()

    private var eventScope: CoroutineScope? = null
    private var eventJob: Job? = null
    private var events: MutableList<AppEvent> = CopyOnWriteArrayList()

    @After
    fun tearDown() {
        eventJob?.cancel()
        eventJob = null
        eventScope?.cancel()
        eventScope = null
        openGates.forEach { it.countDown() }
        openGates.clear()
        viewModelStores.forEach { it.clear() }
        viewModelStores.clear()
        servers.forEach { server -> runCatching { server.shutdown() } }
        servers.clear()
    }

    @Test
    fun linkedProfileSwitchesBetweenAuthenticatedEndpointsWithoutRepairing() {
        val lanServer = startServer()
        val remoteServer = startServer()
        val lanOrigin = originOf(lanServer)
        val remoteOrigin = originOf(remoteServer)
        val lanLog = CopyOnWriteArrayList<String>()
        val remoteLog = CopyOnWriteArrayList<String>()
        lanServer.dispatcher = remoteDispatcher(lan = lanOrigin, remote = remoteOrigin, log = lanLog)
        remoteServer.dispatcher = remoteDispatcher(lan = lanOrigin, remote = remoteOrigin, log = remoteLog)

        val harness = installHarness(SavedConnection(lanOrigin, INSTANCE_ID, "测试电脑"), SYNTHETIC_TOKEN)
        val vm = harness.viewModel
        awaitReady(vm)
        assertEquals(ConnectionMode.LAN, vm.state.value.connectionMode)
        val registered = requireNotNull(harness.connectionStore.read())
        assertEquals(lanOrigin, registered.lanBaseUrl)
        assertEquals(remoteOrigin, registered.remoteBaseUrl)
        assertTrue("Bearer 认证后应登记 remote-v1", registered.supportsRemoteMedia)

        onMain { vm.switchConnectionMode(ConnectionMode.REMOTE) }
        val remoteReady = awaitState(vm, "远程入口就绪") {
            it.phase == ConnectionPhase.READY && it.connectionMode == ConnectionMode.REMOTE
        }
        assertEquals(remoteOrigin, remoteReady.connectionOrigin)
        assertTrue(
            "远程状态必须带当前 origin 的 Bearer：$remoteLog",
            remoteLog.any { it == "GET /api/v1/status auth=Bearer $SYNTHETIC_TOKEN" },
        )
        assertFalse("已登记入口切换不得重新配对", lanLog.any { it.startsWith("POST /api/v1/pair-requests") })
        assertFalse("已登记入口切换不得重新配对", remoteLog.any { it.startsWith("POST /api/v1/pair-requests") })

        // 从持久化档案重启：应直接在 REMOTE 入口就绪，不重新配对。
        val restarted = newViewModel(harness)
        awaitState(restarted, "重启后自动恢复远程入口") {
            it.phase == ConnectionPhase.READY && it.connectionMode == ConnectionMode.REMOTE
        }

        onMain { restarted.switchConnectionMode(ConnectionMode.LAN) }
        val back = awaitState(restarted, "切回局域网") {
            it.phase == ConnectionPhase.READY && it.connectionMode == ConnectionMode.LAN
        }
        assertEquals(lanOrigin, back.connectionOrigin)
        assertEquals(lanOrigin, harness.connectionStore.read()?.baseUrl)
        assertFalse(lanLog.any { it.startsWith("POST /api/v1/pair-requests") })
        assertFalse(remoteLog.any { it.startsWith("POST /api/v1/pair-requests") })
    }

    @Test
    fun forgedProfileWithMatchingInstanceIdCannotKeepToken() {
        val server = startServer()
        val origin = originOf(server)
        server.dispatcher = remoteDispatcher(lan = origin, remote = origin)
        val harness = installHarness(SavedConnection(origin, INSTANCE_ID, "测试电脑"), SYNTHETIC_TOKEN)
        awaitReady(harness.viewModel)
        assertTrue(harness.repository.hasToken())

        onMain {
            harness.repository.selectConnection(
                SavedConnection(
                    baseUrl = "http://10.9.9.9:38522",
                    instanceId = INSTANCE_ID,
                    displayName = "Fake PC",
                    mode = ConnectionMode.REMOTE,
                    remoteBaseUrl = "http://10.9.9.9:38522",
                ),
            )
        }

        assertFalse("伪造的 next 别名绝不能移动令牌", harness.repository.hasToken())
    }

    @Test
    fun transientRemoteFailureKeepsTokenAndDoesNotFallBackToDiscovery() {
        val lanServer = startServer()
        val remoteServer = startServer()
        val lanOrigin = originOf(lanServer)
        val remoteOrigin = originOf(remoteServer)
        lanServer.dispatcher = remoteDispatcher(lan = lanOrigin, remote = remoteOrigin)
        remoteServer.dispatcher = remoteDispatcher(lan = lanOrigin, remote = remoteOrigin, statusCode = 500)

        val harness = installHarness(
            SavedConnection(remoteOrigin, INSTANCE_ID, "测试电脑", ConnectionMode.REMOTE, lanOrigin, remoteOrigin, true),
            SYNTHETIC_TOKEN,
        )
        val failed = awaitState(harness.viewModel, "远程恢复失败界面") {
            it.phase == ConnectionPhase.DISCOVERY && it.remoteRestoreFailed
        }

        assertEquals(ConnectionMode.REMOTE, failed.connectionMode)
        assertEquals(remoteOrigin, failed.connectionOrigin)
        assertFalse("远程离线不得回退到 UDP 发现", failed.isDiscovering)
        assertNull(failed.pairingCode)
        assertTrue("超时/离线必须保留令牌", harness.repository.hasToken())

        onMain { harness.viewModel.setDiscoveryMode(ConnectionMode.LAN) }
        val recovered = awaitState(harness.viewModel, "手工选择局域网复用已登记入口") {
            it.phase == ConnectionPhase.READY && it.connectionMode == ConnectionMode.LAN
        }
        assertEquals(lanOrigin, recovered.connectionOrigin)
        assertFalse(recovered.remoteRestoreFailed)
        assertTrue(harness.repository.hasToken())
    }

    @Test
    fun unauthorizedRestoreEntersPairing() {
        val server = startServer()
        val origin = originOf(server)
        server.dispatcher = remoteDispatcher(lan = origin, remote = origin, statusCode = 401)

        val harness = installHarness(SavedConnection(origin, INSTANCE_ID, "测试电脑"), SYNTHETIC_TOKEN)
        val pairing = awaitState(harness.viewModel, "401 进入配对") { it.phase == ConnectionPhase.PAIRING }

        assertNotNull(pairing.pairingCode)
    }

    @Test
    fun modeSwitchKeepsDraftAndInvalidatesInFlightSearch() {
        val pageGate = CountDownLatch(1)
        openGates += pageGate
        val pageReached = CountDownLatch(1)
        val createCount = AtomicInteger(0)
        val lanServer = startServer()
        val remoteServer = startServer()
        val lanOrigin = originOf(lanServer)
        val remoteOrigin = originOf(remoteServer)
        lanServer.dispatcher = remoteDispatcher(lan = lanOrigin, remote = remoteOrigin) { request ->
            when {
                request.method == "POST" && request.path == "/api/v1/searches" -> {
                    createCount.incrementAndGet()
                    jsonResponse(201, """{"search_id":"search-A"}""")
                }
                request.method == "GET" && request.path?.startsWith("/api/v1/searches/search-A") == true -> {
                    pageReached.countDown()
                    if (pageGate.await(10, TimeUnit.SECONDS)) {
                        jsonResponse(200, pageBody("search-A", 1, ITEM_COUNT, itemsJson(ITEM_COUNT)))
                    } else {
                        MockResponse().setResponseCode(504)
                    }
                }
                else -> null
            }
        }
        remoteServer.dispatcher = remoteDispatcher(lan = lanOrigin, remote = remoteOrigin)

        val harness = installHarness(
            SavedConnection(lanOrigin, INSTANCE_ID, "测试电脑", ConnectionMode.LAN, lanOrigin, remoteOrigin, true),
            SYNTHETIC_TOKEN,
        )
        val vm = harness.viewModel
        awaitReady(vm)
        onMain { vm.setSearchText("cat") }
        onMain { vm.search() }
        assertTrue("搜索分页请求未到达", pageReached.await(10, TimeUnit.SECONDS))

        onMain { vm.switchConnectionMode(ConnectionMode.REMOTE) }
        val switched = awaitState(vm, "切换远程后进入就绪") {
            it.phase == ConnectionPhase.READY && it.connectionMode == ConnectionMode.REMOTE
        }
        assertEquals("草稿文字必须保留", "cat", switched.searchText)
        assertTrue("旧地址结果必须清空", switched.results.isEmpty())
        assertTrue("旧推荐必须清空", switched.recommendations.items.isEmpty())
        assertNull(switched.activeSearchId)

        pageGate.countDown()
        Thread.sleep(500)
        val afterLate = vm.state.value
        assertTrue("旧地址的迟到搜索不得写入结果", afterLate.results.isEmpty())
        assertNull(afterLate.activeSearchId)
        assertEquals(ConnectionMode.REMOTE, afterLate.connectionMode)
        assertEquals("切模式不得自动重新搜索", 1, createCount.get())
    }

    @Test
    fun transferBusyBlocksModeSwitch() {
        val downloadGate = CountDownLatch(1)
        openGates += downloadGate
        val downloadReached = CountDownLatch(1)
        val lanServer = startServer()
        val remoteServer = startServer()
        val lanOrigin = originOf(lanServer)
        val remoteOrigin = originOf(remoteServer)
        lanServer.dispatcher = remoteDispatcher(lan = lanOrigin, remote = remoteOrigin) { request ->
            if (request.method == "GET" && request.path == "/api/v1/media/m0/original") {
                downloadReached.countDown()
                if (downloadGate.await(10, TimeUnit.SECONDS)) {
                    bytesResponse(ByteArray(1024) { (it % 251).toByte() })
                } else {
                    MockResponse().setResponseCode(504)
                }
            } else {
                null
            }
        }
        remoteServer.dispatcher = remoteDispatcher(lan = lanOrigin, remote = remoteOrigin)

        val harness = installHarness(
            SavedConnection(lanOrigin, INSTANCE_ID, "测试电脑", ConnectionMode.LAN, lanOrigin, remoteOrigin, true),
            SYNTHETIC_TOKEN,
        )
        val vm = harness.viewModel
        awaitReady(vm)
        startEventCollection(vm)

        val destination = newOutputFile("mode-switch-save")
        onMain {
            vm.saveItem(
                SearchItem(mediaId = "m0", name = "i.jpg", width = 10, height = 10, libraryName = "图库"),
                Uri.fromFile(destination),
            )
        }
        assertTrue("保存请求未到达", downloadReached.await(10, TimeUnit.SECONDS))
        awaitState(vm, "保存进行中") { it.transferAttempt != null }

        onMain { vm.switchConnectionMode(ConnectionMode.REMOTE) }
        Thread.sleep(200)
        assertEquals("传输中不得切换连接方式", ConnectionMode.LAN, vm.state.value.connectionMode)
        assertEquals(lanOrigin, harness.connectionStore.read()?.baseUrl)
        assertTrue(messageEvents("正在传输文件，暂时无法切换连接方式") >= 1)

        onMain { vm.cancelTransfer() }
        downloadGate.countDown()
    }

    @Test
    fun statusWithMismatchedAdvertisedEndpointsKeepsPreviousProfileAndToken() {
        val server = startServer()
        val origin = originOf(server)
        // The desktop advertises aliases that do not include this session's own origin.
        server.dispatcher = remoteDispatcher(
            lan = "http://192.168.1.10:38522",
            remote = "http://203.0.113.7:38522",
        )
        val harness = installHarness(
            SavedConnection(origin, INSTANCE_ID, "旧名字", ConnectionMode.LAN, null, null, false),
            SYNTHETIC_TOKEN,
        )
        awaitState(harness.viewModel, "状态就绪") { it.phase == ConnectionPhase.READY }

        val saved = requireNotNull(harness.connectionStore.read())
        assertEquals("不匹配的 advertised 入口不得改写已保存档案", "旧名字", saved.displayName)
        assertNull("不匹配时不得登记 lan 别名", saved.lanBaseUrl)
        assertNull("不匹配时不得登记 remote 别名", saved.remoteBaseUrl)
        assertEquals(ConnectionMode.LAN, saved.mode)
        assertTrue("匹配失败必须保留令牌", harness.repository.hasToken())
    }

    @Test
    fun legacyStatusWithoutEndpointsKeepsExplicitRemoteModeForPrivateOrigin() {
        // A manually chosen REMOTE entry may point at a private/VPN test origin with no aliases yet.
        // A legacy desktop that returns no connection_endpoints must not reclassify it back to LAN.
        val server = startServer()
        val privateOrigin = originOf(server)
        server.dispatcher = remoteDispatcher(lan = privateOrigin, remote = privateOrigin) { request ->
            if (request.method == "GET" && request.path == "/api/v1/status") {
                jsonResponse(200, """{"protocol":1,"instance_id":"$INSTANCE_ID","name":"测试电脑"}""")
            } else null
        }
        val harness = installHarness(
            SavedConnection(
                baseUrl = privateOrigin,
                instanceId = INSTANCE_ID,
                displayName = "旧名字",
                mode = ConnectionMode.REMOTE,
                lanBaseUrl = null,
                remoteBaseUrl = null,
                supportsRemoteMedia = false,
            ),
            SYNTHETIC_TOKEN,
        )
        awaitReady(harness.viewModel)
        onMain {
            harness.repository.registerAuthenticatedEndpoints(
                StatusResponse(
                    protocol = 1,
                    instanceId = INSTANCE_ID,
                    name = "测试电脑",
                    connectionEndpoints = null,
                    mediaProfiles = emptyList(),
                ),
            )
        }

        val saved = requireNotNull(harness.connectionStore.read())
        assertEquals(
            "无 connection_endpoints 时必须保留用户显式选择的 REMOTE",
            ConnectionMode.REMOTE,
            saved.mode,
        )
        assertNull(saved.lanBaseUrl)
        assertNull(saved.remoteBaseUrl)
        assertTrue("同一 origin 的令牌必须保留", harness.repository.hasToken())
    }

    @Test
    fun modeSwitchInvalidatesInFlightUploadAndPreservesDraftImage() {
        val uploadGate = CountDownLatch(1)
        openGates += uploadGate
        val uploadReached = CountDownLatch(1)
        val lanUploads = AtomicInteger(0)
        val remoteUploads = AtomicInteger(0)
        val lanServer = startServer()
        val remoteServer = startServer()
        val lanOrigin = originOf(lanServer)
        val remoteOrigin = originOf(remoteServer)
        lanServer.dispatcher = remoteDispatcher(lan = lanOrigin, remote = remoteOrigin) { request ->
            if (request.method == "POST" && request.requestUrl?.encodedPath == "/api/v1/query-images") {
                lanUploads.incrementAndGet()
                uploadReached.countDown()
                if (uploadGate.await(10, TimeUnit.SECONDS)) {
                    jsonResponse(201, """{"query_image_id":"q-lan","name":"query.jpg"}""")
                } else {
                    MockResponse().setResponseCode(504)
                }
            } else {
                null
            }
        }
        remoteServer.dispatcher = remoteDispatcher(lan = lanOrigin, remote = remoteOrigin) { request ->
            if (request.method == "POST" && request.requestUrl?.encodedPath == "/api/v1/query-images") {
                remoteUploads.incrementAndGet()
                jsonResponse(201, """{"query_image_id":"q-remote","name":"query.jpg"}""")
            } else {
                null
            }
        }

        val harness = installHarness(
            SavedConnection(lanOrigin, INSTANCE_ID, "测试电脑", ConnectionMode.LAN, lanOrigin, remoteOrigin, true),
            SYNTHETIC_TOKEN,
        )
        val vm = harness.viewModel
        awaitReady(vm)

        val queryUri = writeTempQueryFile()
        onMain { vm.selectQueryImage(queryUri) }
        assertTrue("上传请求未到达局域网 origin", uploadReached.await(10, TimeUnit.SECONDS))
        val beforeSwitch = requireNotNull(vm.state.value.queryImage)

        onMain { vm.switchConnectionMode(ConnectionMode.REMOTE) }
        val switched = awaitState(vm, "切换远程后进入就绪") {
            it.phase == ConnectionPhase.READY && it.connectionMode == ConnectionMode.REMOTE
        }
        val preserved = requireNotNull(switched.queryImage)
        assertEquals("切模式必须保留所选查询图片", queryUri, preserved.uri)
        assertEquals("切模式必须保留文件名", beforeSwitch.displayName, preserved.displayName)
        assertEquals("切模式必须保留 MIME", beforeSwitch.mimeType, preserved.mimeType)
        assertNull("旧 origin 的 query_image_id 必须清除", preserved.queryImageId)
        assertFalse(preserved.uploading)
        assertEquals(0L, preserved.bytesUploaded)
        assertNotNull("必须有重新上传提示，重试按钮才可用", preserved.uploadError)

        // Release the delayed old callback: it must never apply over the switched profile.
        uploadGate.countDown()
        Thread.sleep(500)
        val afterLate = vm.state.value
        assertEquals(queryUri, afterLate.queryImage?.uri)
        assertNull("旧回包不得写回 query_image_id", afterLate.queryImage?.queryImageId)
        assertEquals("旧回包不得写入进度", 0L, afterLate.queryImage?.bytesUploaded)
        assertEquals(ConnectionMode.REMOTE, afterLate.connectionMode)
        assertEquals("旧 origin 不得重复上传", 1, lanUploads.get())

        onMain { vm.retryQueryUpload() }
        val retried = awaitState(vm, "在新 origin 显式重试上传成功") {
            it.queryImage?.queryImageId == "q-remote"
        }
        assertNull(retried.queryImage!!.uploadError)
        assertFalse(retried.queryImage!!.uploading)
        assertTrue("重试必须打到当前 origin", remoteUploads.get() >= 1)
    }

    // --- 测试基建 ---------------------------------------------------------------

    private class Harness(
        val context: Context,
        val connectionStore: ConnectionStore,
        val repository: ZvecRepository,
        val savedFilesStore: SavedFilesStore,
        val viewModel: AppViewModel,
        val savedStateHandle: SavedStateHandle,
    )

    private fun startServer(): MockWebServer = MockWebServer().apply { start() }.also { servers += it }

    private fun originOf(server: MockWebServer): String = "http://127.0.0.1:${server.port}"

    private fun installHarness(
        savedConnection: SavedConnection,
        token: String?,
        savedStateHandle: SavedStateHandle = SavedStateHandle(),
    ): Harness {
        val context = IsolatedContext(targetContext)
        val connectionStore = ConnectionStore(context)
        connectionStore.write(savedConnection)
        val tokenStore = InMemoryTokenStore(token)
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
        val savedFilesStore = SavedFilesStore(context)
        val store = ViewModelStore()
        viewModelStores += store
        val viewModel = createViewModel(store, context, repository, savedFilesStore, savedStateHandle)
        return Harness(context, connectionStore, repository, savedFilesStore, viewModel, savedStateHandle)
    }

    private fun newViewModel(harness: Harness): AppViewModel {
        val store = ViewModelStore()
        viewModelStores += store
        return createViewModel(
            store = store,
            context = harness.context,
            repository = harness.repository,
            savedFilesStore = harness.savedFilesStore,
            savedStateHandle = harness.savedStateHandle,
        )
    }

    private fun createViewModel(
        store: ViewModelStore,
        context: Context,
        repository: ZvecRepository,
        savedFilesStore: SavedFilesStore,
        savedStateHandle: SavedStateHandle,
    ): AppViewModel {
        val factory = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T {
                require(modelClass.isAssignableFrom(AppViewModel::class.java)) { "意外的 ViewModel 类型" }
                return AppViewModel(context, repository, savedFilesStore, savedStateHandle) as T
            }
        }
        var created: AppViewModel? = null
        onMain { created = ViewModelProvider(store, factory)[AppViewModel::class.java] }
        return requireNotNull(created)
    }

    private class IsolatedContext(base: Context) : ContextWrapper(base) {
        private val prefix = "zvec-remote-instrumented-${UUID.randomUUID()}"
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

    private fun awaitReady(vm: AppViewModel) {
        awaitState(vm, "连接就绪且图库已加载") { it.phase == ConnectionPhase.READY && it.libraries.size == 2 }
    }

    private fun awaitState(
        vm: AppViewModel,
        description: String,
        timeoutMillis: Long = 10_000,
        predicate: (AppUiState) -> Boolean,
    ): AppUiState {
        val deadline = SystemClock.uptimeMillis() + timeoutMillis
        while (SystemClock.uptimeMillis() < deadline) {
            val state = vm.state.value
            if (predicate(state)) return state
            Thread.sleep(20)
        }
        throw AssertionError("等待状态超时：$description；当前状态=${vm.state.value}")
    }

    private fun startEventCollection(vm: AppViewModel) {
        events = CopyOnWriteArrayList()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        eventScope = scope
        val subscribed = CountDownLatch(1)
        eventJob = scope.launch {
            vm.events.onSubscription { subscribed.countDown() }.collect { events.add(it) }
        }
        assertTrue("事件订阅未建立", subscribed.await(5, TimeUnit.SECONDS))
    }

    private fun messageEvents(text: String): Int = events.count { it is AppEvent.Message && it.text == text }

    private fun newOutputFile(label: String): File {
        val directory = File(targetContext.cacheDir, "zvec-test-output/$label-${UUID.randomUUID()}").apply { mkdirs() }
        return File(directory, "$label.bin")
    }

    private fun writeTempQueryFile(): Uri {
        val directory = File(targetContext.cacheDir, "zvec-test-query-${UUID.randomUUID()}").apply { mkdirs() }
        val file = File(directory, "query.jpg").apply { writeBytes(ByteArray(2048) { (it % 97).toByte() }) }
        return Uri.fromFile(file)
    }

    private fun remoteDispatcher(
        lan: String?,
        remote: String?,
        statusCode: Int = 200,
        log: MutableList<String>? = null,
        extra: ((RecordedRequest) -> MockResponse?)? = null,
    ): Dispatcher = object : Dispatcher() {
        override fun dispatch(request: RecordedRequest): MockResponse {
            val path = request.requestUrl?.encodedPath ?: return MockResponse().setResponseCode(404)
            log?.add("${request.method} $path auth=${request.getHeader("Authorization") ?: "-"}")
            extra?.invoke(request)?.let { return it }
            return when {
                request.method == "GET" && path == "/api/v1/status" ->
                    if (statusCode == 200) {
                        jsonResponse(200, statusJson(INSTANCE_ID, lan, remote))
                    } else {
                        jsonResponse(statusCode, errorBody("unauthorized", "凭据已失效"))
                    }
                request.method == "GET" && path == "/api/v1/libraries" -> jsonResponse(200, LIBRARIES_JSON)
                request.method == "DELETE" -> jsonResponse(200, "{}")
                request.method == "POST" && path == "/api/v1/pair-requests" -> jsonResponse(201, PAIR_START_JSON)
                request.method == "POST" && path.endsWith("/poll") -> jsonResponse(200, """{"status":"pending"}""")
                else -> MockResponse().setResponseCode(404)
            }
        }
    }

    private fun statusJson(instanceId: String, lan: String?, remote: String?): String {
        val endpoints = buildString {
            append("{\"lan\":")
            append(if (lan == null) "null" else "\"$lan\"")
            append(",\"remote\":")
            append(if (remote == null) "null" else "\"$remote\"")
            append("}")
        }
        return """{"protocol":1,"instance_id":"$instanceId","name":"测试电脑",""" +
            """"connection_endpoints":$endpoints,"media_profiles":["remote-v1"]}"""
    }

    private fun jsonResponse(code: Int, body: String): MockResponse =
        MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(body)

    private fun bytesResponse(bytes: ByteArray): MockResponse =
        MockResponse().setResponseCode(200).setHeader("Content-Type", "image/jpeg").setBody(Buffer().write(bytes))

    private fun errorBody(code: String, message: String): String = """{"error":{"code":"$code","message":"$message"}}"""

    private fun itemsJson(count: Int): String = (0 until count).joinToString(",") { index ->
        """{"media_id":"media-$index","name":"item-$index.jpg","width":640,"height":480,"tags":[],"library_id":"lib-1","library_name":"图库一","content_type":"image/jpeg","size_bytes":1024}"""
    }

    private fun pageBody(searchId: String, page: Int, total: Int, items: String): String =
        """{"search_id":"$searchId","page":$page,"page_size":100,"total":$total,"items":[$items]}"""

    private companion object {
        const val INSTANCE_ID = "inst-remote"
        const val SYNTHETIC_TOKEN = "synthetic-remote-instrumented-token"
        const val ITEM_COUNT = 3
        const val LIBRARIES_JSON =
            """{"libraries":[{"id":"lib-1","name":"图库一","count":3},{"id":"lib-2","name":"图库二","count":1}]}"""
        const val PAIR_START_JSON =
            """{"pairing_id":"p1","comparison_code":"123456","expires_in_seconds":300,"expires_at":null,"status":"pending"}"""
    }
}
