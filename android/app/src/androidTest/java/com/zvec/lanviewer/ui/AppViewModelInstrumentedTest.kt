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
import com.zvec.lanviewer.data.local.ConnectionStore
import com.zvec.lanviewer.data.local.InstallationStore
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.local.SavedFilesStore
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.SearchMode
import com.zvec.lanviewer.data.network.LanApiClient
import com.zvec.lanviewer.data.network.ResumableDownloader
import com.zvec.lanviewer.data.repository.ZvecRepository
import com.zvec.lanviewer.data.security.InMemoryTokenStore
import java.io.File
import java.security.MessageDigest
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
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * A 阶段真实调用链验收：在模拟器自身用 MockWebServer 承载真实 [ZvecRepository] / [AppViewModel]。
 *
 * 每个用例都只通过生产入口操作，状态断言在独立测试线程上有界等待；Viewmodel 入口统一在主线程执行。
 * 所有偏好/文件/缓存都在测试专属 [IsolatedContext] 下，不触碰用户库、连接、缓存或 Keystore。
 */
@RunWith(AndroidJUnit4::class)
class AppViewModelInstrumentedTest {
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
    fun firstPageSucceedsAndNextPageFailureIsVisibleThenExplicitRetryLoadsFollowingItems() {
        val page2Attempts = AtomicInteger(0)
        val harness = installHarness(object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                commonResponse(request)?.let { return it }
                val url = request.requestUrl!!
                val path = url.encodedPath
                return when {
                    request.method == "POST" && path == "/api/v1/searches" ->
                        jsonResponse(201, """{"search_id":"$SEARCH_ID_A"}""")
                    request.method == "GET" && path == "/api/v1/searches/$SEARCH_ID_A" ->
                        if (url.queryParameter("page")?.toInt() == 1) {
                            jsonResponse(200, pageBody(SEARCH_ID_A, 1, 250, itemsJson("a", 100)))
                        } else if (page2Attempts.incrementAndGet() == 1) {
                            jsonResponse(500, errorBody("search_busy", "电脑暂时无法完成搜索"))
                        } else {
                            jsonResponse(200, pageBody(SEARCH_ID_A, 2, 250, itemsJson("a", 100, 100)))
                        }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        })
        val vm = harness.viewModel
        awaitReady(vm)

        onMain { vm.setSearchText("cat") }
        onMain { vm.search() }
        val firstPage = awaitState(vm, description = "第一页 100 项加载完成") { it.results.size == 100 }
        assertEquals(SEARCH_ID_A, firstPage.activeSearchId)
        assertEquals(2, firstPage.nextPage)
        assertEquals(250, firstPage.totalResults)
        assertEquals(SearchPhase.LOADED, firstPage.searchPhase)

        onMain { vm.loadNextPage() }
        val failed = awaitState(vm, description = "下一页失败可见") { it.nextPageError != null }
        assertEquals(100, failed.results.size)
        assertEquals(2, failed.nextPage)
        assertEquals("电脑暂时无法完成搜索", failed.nextPageError)

        onMain { vm.retryNextPage() }
        val retried = awaitState(vm, description = "显式重试后得到后续项") { it.results.size == 200 }
        assertEquals(3, retried.nextPage)
        assertNull(retried.nextPageError)
        assertEquals(250, retried.totalResults)
        assertEquals(SEARCH_ID_A, retried.activeSearchId)
        assertEquals("a-199.jpg", retried.results.last().name)
    }

    @Test
    fun concurrentLoadNextPageDoesNotIssueDuplicateRequestForSamePage() {
        val page2Requests = AtomicInteger(0)
        val page2Reached = CountDownLatch(1)
        val page2Gate = CountDownLatch(1)
        openGates += page2Gate
        val harness = installHarness(object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                commonResponse(request)?.let { return it }
                val url = request.requestUrl!!
                val path = url.encodedPath
                return when {
                    request.method == "POST" && path == "/api/v1/searches" ->
                        jsonResponse(201, """{"search_id":"$SEARCH_ID_A"}""")
                    request.method == "GET" && path == "/api/v1/searches/$SEARCH_ID_A" ->
                        if (url.queryParameter("page")?.toInt() == 1) {
                            jsonResponse(200, pageBody(SEARCH_ID_A, 1, 200, itemsJson("a", 100)))
                        } else {
                            page2Requests.incrementAndGet()
                            page2Reached.countDown()
                            page2Gate.await(10, TimeUnit.SECONDS)
                            jsonResponse(200, pageBody(SEARCH_ID_A, 2, 200, itemsJson("a", 100, 100)))
                        }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        })
        val vm = harness.viewModel
        awaitReady(vm)
        onMain { vm.setSearchText("cat") }
        onMain { vm.search() }
        awaitState(vm, description = "第一页完成") { it.results.size == 100 }

        onMain { vm.loadNextPage() }
        assertTrue("下一页请求未到达", page2Reached.await(10, TimeUnit.SECONDS))
        // 同一页仍在加载时再次触发，必须被 canStartNextPage 拦截，不能再发一次相同页请求。
        onMain { vm.loadNextPage() }
        page2Gate.countDown()

        val state = awaitState(vm, description = "下一页完成且未重复请求") { it.results.size == 200 }
        assertEquals("同一页不得并发重复请求", 1, page2Requests.get())
        assertEquals(3, state.nextPage)
    }

    @Test
    fun cancelledSearchCannotBeOverwrittenByItsLateResponseNorStartAnotherSearch() {
        val createCount = AtomicInteger(0)
        val searchARequested = CountDownLatch(1)
        val gateA = CountDownLatch(1)
        openGates += gateA
        val harness = installHarness(object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                commonResponse(request)?.let { return it }
                val path = request.requestUrl!!.encodedPath
                return when {
                    request.method == "POST" && path == "/api/v1/searches" -> {
                        val index = createCount.incrementAndGet()
                        jsonResponse(201, """{"search_id":"${if (index == 1) SEARCH_ID_A else SEARCH_ID_B}"}""")
                    }
                    request.method == "GET" && path == "/api/v1/searches/$SEARCH_ID_A" -> {
                        searchARequested.countDown()
                        gateA.await(10, TimeUnit.SECONDS)
                        jsonResponse(200, pageBody(SEARCH_ID_A, 1, 1, itemsJson("a", 1)))
                    }
                    request.method == "GET" && path == "/api/v1/searches/$SEARCH_ID_B" ->
                        jsonResponse(200, pageBody(SEARCH_ID_B, 1, 1, itemsJson("b", 1)))
                    else -> MockResponse().setResponseCode(404)
                }
            }
        })
        val vm = harness.viewModel
        awaitReady(vm)
        onMain { vm.setSearchText("cat") }
        onMain { vm.search() }
        assertTrue("查询 A 未到达服务端", searchARequested.await(10, TimeUnit.SECONDS))

        onMain { vm.cancelSearch() }
        val cancelled = awaitState(vm, description = "取消后进入已取消状态") { it.searchPhase == SearchPhase.CANCELLED }
        assertNull("旧搜索 ID 不得保留", cancelled.activeSearchId)
        assertEquals(1, createCount.get())
        Thread.sleep(200)
        assertEquals("取消不得自动发起新搜索", 1, createCount.get())

        onMain { vm.search() }
        val bState = awaitState(vm, description = "查询 B 首页完成") {
            it.activeSearchId == SEARCH_ID_B && it.results.size == 1
        }
        assertEquals("b-0.jpg", bState.results[0].name)
        assertEquals(2, bState.nextPage)
        assertNull(bState.nextPageError)
        assertNull(bState.searchError)
        assertFalse(bState.isLoadingNextPage)
        assertEquals(SearchPhase.LOADED, bState.searchPhase)

        // A 的迟到成功响应不得改变 B 的结果、错误或 loading。
        gateA.countDown()
        Thread.sleep(400)
        val afterLate = vm.state.value
        assertEquals(SEARCH_ID_B, afterLate.activeSearchId)
        assertEquals(1, afterLate.results.size)
        assertEquals("b-0.jpg", afterLate.results[0].name)
        assertNull(afterLate.nextPageError)
        assertNull(afterLate.searchError)
        assertFalse(afterLate.isLoadingNextPage)
    }

    @Test
    fun failedQueryImageBlocksSilentTextSearchAndRetrySendsCombinedRequestWithImageId() {
        val uploadAttempts = AtomicInteger(0)
        val createBodies = CopyOnWriteArrayList<String>()
        val harness = installHarness(object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                commonResponse(request)?.let { return it }
                val path = request.requestUrl!!.encodedPath
                return when {
                    request.method == "POST" && path == "/api/v1/query-images" ->
                        if (uploadAttempts.incrementAndGet() == 1) {
                            jsonResponse(500, errorBody("upload_failed", "查询图片上传失败"))
                        } else {
                            jsonResponse(200, """{"query_image_id":"q-1","name":"query.jpg"}""")
                        }
                    request.method == "POST" && path == "/api/v1/searches" -> {
                        createBodies += request.body.readUtf8()
                        jsonResponse(201, """{"search_id":"$SEARCH_ID_A"}""")
                    }
                    request.method == "GET" && path == "/api/v1/searches/$SEARCH_ID_A" ->
                        jsonResponse(200, pageBody(SEARCH_ID_A, 1, 1, itemsJson("x", 1)))
                    else -> MockResponse().setResponseCode(404)
                }
            }
        })
        val vm = harness.viewModel
        awaitReady(vm)
        onMain { vm.setSearchText("cat") }

        val queryFile = writeTempFile("query-image", QUERY_IMAGE_BYTES)
        val queryUri = Uri.fromFile(queryFile)
        onMain { vm.selectQueryImage(queryUri) }

        val failedUpload = awaitState(vm, description = "首次上传失败且保留附件") {
            val image = it.queryImage
            image != null && !image.uploading && image.uploadError != null
        }
        assertNotNull(failedUpload.queryImage)
        assertEquals(queryUri, failedUpload.queryImage!!.uri)
        assertNull("上传失败不得存在成功 ID", failedUpload.queryImage!!.queryImageId)
        assertEquals(1, uploadAttempts.get())

        // 文本 + 失败图不能静默降级为纯文本搜索。
        onMain { vm.search() }
        val blocked = vm.state.value
        assertNotNull("文本+失败图必须被拦截", blocked.errorMessage)
        assertEquals(SearchPhase.IDLE, blocked.searchPhase)
        assertTrue("失败图不得触发 createSearch", createBodies.isEmpty())

        onMain { vm.retryQueryUpload() }
        val uploaded = awaitState(vm, description = "重试上传成功") { it.queryImage?.queryImageId == "q-1" }
        assertNull(uploaded.queryImage!!.uploadError)
        assertFalse(uploaded.queryImage!!.uploading)

        onMain { vm.search() }
        awaitState(vm, description = "图文联合搜索完成") { it.results.size == 1 }
        assertEquals(1, createBodies.size)
        val body = createBodies.single()
        assertTrue("联合请求必须携带 query_image_id，实际=$body", body.contains("\"query_image_id\":\"q-1\""))
        assertTrue("联合请求模式必须是 combined，实际=$body", body.contains("\"mode\":\"combined\""))
        assertTrue("联合请求必须保留文本，实际=$body", body.contains("\"text\":\"cat\""))
    }

    @Test
    fun saveBindsToCapturedItemAndSurvivesSwitchingItemAndClosingViewer() {
        val downloadReached = CountDownLatch(1)
        val downloadGate = CountDownLatch(1)
        openGates += downloadGate
        val harness = installHarness(object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                commonResponse(request)?.let { return it }
                val path = request.requestUrl!!.encodedPath
                return when {
                    request.method == "POST" && path == "/api/v1/searches" ->
                        jsonResponse(201, """{"search_id":"$SEARCH_ID_A"}""")
                    request.method == "GET" && path == "/api/v1/searches/$SEARCH_ID_A" ->
                        jsonResponse(200, pageBody(SEARCH_ID_A, 1, 2, itemsJson("item", 2)))
                    request.method == "GET" && path == "/api/v1/media/item-0/original" -> {
                        downloadReached.countDown()
                        downloadGate.await(10, TimeUnit.SECONDS)
                        bytesResponse(ORIGINAL_BYTES)
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        })
        val vm = harness.viewModel
        awaitReady(vm)
        onMain { vm.setSearchText("cat") }
        onMain { vm.search() }
        val loaded = awaitState(vm, description = "两项目结果加载完成") { it.results.size == 2 }

        onMain { vm.openViewer(1) }
        awaitState(vm, description = "查看第二项") { it.viewerIndex == 1 }

        val destinationA = newOutputFile("save-a")
        val destinationB = newOutputFile("save-b")
        startEventCollection(vm)

        onMain { vm.saveItem(loaded.results[0], Uri.fromFile(destinationA)) }
        assertTrue("原图请求未到达", downloadReached.await(10, TimeUnit.SECONDS))
        val inProgress = awaitState(vm, description = "保存任务进行中") { it.transferAttempt != null }
        assertTrue(inProgress.transferMessage!!.contains("item-0.jpg"))

        // 同一帧的第二个保存不得取消第一个。
        val attemptBefore = vm.state.value.transferAttempt
        onMain { vm.saveItem(loaded.results[1], Uri.fromFile(destinationB)) }
        assertEquals(attemptBefore, vm.state.value.transferAttempt)
        assertTrue(vm.state.value.savedFiles.isEmpty())
        // 同一帧的分享同样不得取消正在进行的保存。
        onMain { vm.shareCurrent() }
        assertEquals(attemptBefore, vm.state.value.transferAttempt)
        assertEquals(0, events.count { it is AppEvent.Share })

        // 关闭查看器后任务进度仍存在。
        onMain { vm.closeViewer() }
        val afterClose = awaitState(vm, description = "关闭查看器后任务仍在") {
            it.viewerIndex == null && it.transferAttempt != null
        }
        assertNotNull(afterClose.transferMessage)

        downloadGate.countDown()
        val finished = awaitState(vm, description = "保存任务结束并清理状态") {
            it.transferAttempt == null && it.savedFiles.size == 1
        }
        assertNull(finished.transferMessage)
        assertEquals("item-0.jpg", finished.savedFiles.single().name)
        assertFalse("同帧第二个目标不得被写入", destinationB.exists())

        assertTrue("保存文件必须存在", destinationA.isFile)
        assertArrayEquals(ORIGINAL_BYTES, destinationA.readBytes())
        assertEquals(sha256(ORIGINAL_BYTES), sha256(destinationA.readBytes()))
        assertEquals(1, harness.savedFilesStore.list().size)
        assertEquals("item-0.jpg", harness.savedFilesStore.list().single().name)
        assertEquals(1, awaitEvent("原图已保存"))
    }

    @Test
    fun cancelledTransferRecordsNothingAndNextTransferStaysClean() {
        val originalA = ByteArray(4096) { (it % 251).toByte() }
        val originalB = ByteArray(3000) { (it % 199).toByte() }
        val aReached = CountDownLatch(1)
        val gateA = CountDownLatch(1)
        openGates += gateA
        val bReached = CountDownLatch(1)
        val gateB = CountDownLatch(1)
        openGates += gateB
        val harness = installHarness(object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                commonResponse(request)?.let { return it }
                val path = request.requestUrl!!.encodedPath
                return when {
                    request.method == "GET" && path == "/api/v1/media/media-a/original" -> {
                        aReached.countDown()
                        gateA.await(10, TimeUnit.SECONDS)
                        bytesResponse(originalA)
                    }
                    request.method == "GET" && path == "/api/v1/media/media-b/original" -> {
                        bReached.countDown()
                        gateB.await(10, TimeUnit.SECONDS)
                        bytesResponse(originalB)
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        })
        val vm = harness.viewModel
        awaitReady(vm)

        val itemA = searchItem("media-a", "a.jpg", originalA.size.toLong())
        val itemB = searchItem("media-b", "b.jpg", originalB.size.toLong())
        val destinationA = newOutputFile("cancel-a")
        val destinationB = newOutputFile("next-b")
        startEventCollection(vm)

        onMain { vm.saveItem(itemA, Uri.fromFile(destinationA)) }
        assertTrue("任务 A 原图请求未到达", aReached.await(10, TimeUnit.SECONDS))
        awaitState(vm, description = "任务 A 进行中") { it.transferAttempt != null }

        onMain { vm.cancelTransfer() }
        awaitState(vm, description = "任务 A 已取消") { it.transferAttempt == null }
        assertTrue("取消不得写入成功记录", harness.savedFilesStore.list().isEmpty())
        assertTrue(vm.state.value.savedFiles.isEmpty())
        assertFalse("取消不得写入目标文件", destinationA.exists())
        assertEquals("取消不得产生成功事件", 0, messageEvents("原图已保存"))

        onMain { vm.saveItem(itemB, Uri.fromFile(destinationB)) }
        assertTrue("任务 B 原图请求未到达", bReached.await(10, TimeUnit.SECONDS))
        val bInProgress = awaitState(vm, description = "任务 B 进行中") { it.transferAttempt != null }
        assertTrue("进行中的必须是任务 B", bInProgress.transferMessage!!.contains("b.jpg"))
        gateB.countDown()

        val finished = awaitState(vm, description = "任务 B 完成") { it.savedFiles.size == 1 }
        assertEquals("b.jpg", finished.savedFiles.single().name)
        assertFalse(destinationA.exists())
        assertTrue(destinationB.isFile)
        assertArrayEquals(originalB, destinationB.readBytes())
        assertEquals(1, harness.savedFilesStore.list().size)
        assertEquals("b.jpg", harness.savedFilesStore.list().single().name)

        // 释放已取消任务 A 的迟到响应，旧进度/旧完成不得覆盖下一任务。
        gateA.countDown()
        Thread.sleep(300)
        assertEquals(1, harness.savedFilesStore.list().size)
        assertEquals("b.jpg", harness.savedFilesStore.list().single().name)
        assertEquals(1, awaitEvent("原图已保存"))
    }

    @Test
    fun newViewModelRestoresOnlySmallDraftAndNeverFakesResultsOrAutoSearch() {
        val createCount = AtomicInteger(0)
        val harness = installHarness(
            dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    commonResponse(request)?.let { return it }
                    val path = request.requestUrl!!.encodedPath
                    return when {
                        request.method == "POST" && path == "/api/v1/searches" -> {
                            createCount.incrementAndGet()
                            jsonResponse(201, """{"search_id":"$SEARCH_ID_A"}""")
                        }
                        request.method == "GET" && path == "/api/v1/searches/$SEARCH_ID_A" ->
                            jsonResponse(200, pageBody(SEARCH_ID_A, 1, 1, itemsJson("x", 1)))
                        else -> MockResponse().setResponseCode(404)
                    }
                }
            },
        )
        val first = harness.viewModel
        awaitReady(first)
        onMain {
            first.setSearchText("日落")
            first.setSearchMode(SearchMode.TAG)
            first.setTopK("48")
            first.toggleLibrary("lib-1")
        }
        onMain { first.search() }
        awaitState(first, description = "首个实例搜索完成") { it.results.size == 1 }
        assertEquals(SEARCH_ID_A, first.state.value.activeSearchId)
        assertEquals(1, createCount.get())

        val restored = newViewModel(harness)
        awaitReady(restored)

        val state = restored.state.value
        assertEquals("日落", state.searchText)
        assertEquals(SearchMode.TAG, state.searchMode)
        assertEquals("48", state.topKText)
        assertEquals(setOf("lib-1"), state.selectedLibraryIds)
        assertEquals("上次搜索已结束，请重新搜索", state.searchRestoreNotice)
        assertTrue("结果不得被伪造恢复", state.results.isEmpty())
        assertNull("会话不得被伪造恢复", state.activeSearchId)
        assertEquals(SearchPhase.IDLE, state.searchPhase)
        assertFalse(state.isSearching)
        assertEquals("恢复草稿不得自动 createSearch", 1, createCount.get())
        Thread.sleep(200)
        assertEquals(1, createCount.get())

        // 记录中只能包含小字段，不含结果或 bitmap。
        assertEquals(
            setOf("searchText", "searchMode", "topK", "selectedLibraryIds", "hadSearch", "hadQueryImage"),
            harness.savedStateHandle.keys(),
        )
        assertEquals("日落", harness.savedStateHandle.get<String>("searchText"))
        assertTrue(harness.savedStateHandle.get<Any>("selectedLibraryIds") is ArrayList<*>)
    }

    @Test
    fun newViewModelWithoutSearchPromptsReselectForUnrecoverableQueryImage() {
        val uploadAttempts = AtomicInteger(0)
        val createCount = AtomicInteger(0)
        val harness = installHarness(object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                commonResponse(request)?.let { return it }
                val path = request.requestUrl!!.encodedPath
                return when {
                    request.method == "POST" && path == "/api/v1/query-images" -> {
                        uploadAttempts.incrementAndGet()
                        jsonResponse(500, errorBody("upload_failed", "查询图片上传失败"))
                    }
                    request.method == "POST" && path == "/api/v1/searches" -> {
                        createCount.incrementAndGet()
                        jsonResponse(201, """{"search_id":"$SEARCH_ID_A"}""")
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        })
        val first = harness.viewModel
        awaitReady(first)
        val queryFile = writeTempFile("query-image-restore", QUERY_IMAGE_BYTES)
        onMain {
            first.setSearchText("cat")
            first.selectQueryImage(Uri.fromFile(queryFile))
        }
        awaitState(first, description = "查询图上传失败且被保留") {
            val image = it.queryImage
            image != null && !image.uploading && image.uploadError != null
        }

        val restored = newViewModel(harness)
        awaitReady(restored)

        val state = restored.state.value
        assertEquals("cat", state.searchText)
        assertEquals("上次搜索已结束，请重新选择查询图片并重新搜索", state.searchRestoreNotice)
        assertNull("查询图片不得被伪造恢复", state.queryImage)
        assertTrue(state.results.isEmpty())
        assertNull(state.activeSearchId)
        assertEquals(0, createCount.get())
    }

    // --- 测试基建 ---------------------------------------------------------------

    private class Harness(
        val context: Context,
        val repository: ZvecRepository,
        val savedFilesStore: SavedFilesStore,
        val viewModel: AppViewModel,
        val savedStateHandle: SavedStateHandle,
    )

    private fun installHarness(
        dispatcher: Dispatcher,
        savedStateHandle: SavedStateHandle = SavedStateHandle(),
    ): Harness {
        val context = IsolatedContext(targetContext)
        val server = MockWebServer()
        server.dispatcher = dispatcher
        server.start()
        servers += server

        val baseUrl = server.url("/").toString().trimEnd('/')
        val connectionStore = ConnectionStore(context)
        connectionStore.write(SavedConnection(baseUrl, INSTANCE_ID, "测试电脑"))
        val tokenStore = InMemoryTokenStore(SYNTHETIC_TOKEN)
        val api = LanApiClient(
            connectionReader = connectionStore,
            tokenStore = tokenStore,
            maxConcurrentRequests = LanApiClient.DEFAULT_CONCURRENCY,
        )
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
        return Harness(context, repository, savedFilesStore, viewModel, savedStateHandle)
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

    /** 所有 preferences/files/cache 都落在本测试专属前缀目录，绝不触碰用户数据或 Keystore。 */
    private class IsolatedContext(base: Context) : ContextWrapper(base) {
        private val prefix = "zvec-instrumented-test-${UUID.randomUUID()}"
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
        awaitState(vm, description = "连接就绪且图库已加载") {
            it.phase == ConnectionPhase.READY && it.libraries.size == 2
        }
    }

    private fun awaitState(
        vm: AppViewModel,
        timeoutMillis: Long = 10_000,
        description: String,
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

    private fun messageEvents(text: String): Int =
        events.count { it is AppEvent.Message && it.text == text }

    private fun awaitEvent(text: String, timeoutMillis: Long = 5_000): Int {
        val deadline = SystemClock.uptimeMillis() + timeoutMillis
        while (SystemClock.uptimeMillis() < deadline) {
            val count = messageEvents(text)
            if (count > 0) return count
            Thread.sleep(20)
        }
        throw AssertionError("等待事件超时：$text；当前事件=$events")
    }

    private fun commonResponse(request: RecordedRequest): MockResponse? {
        val path = request.requestUrl?.encodedPath ?: return null
        return when {
            request.method == "GET" && path == "/api/v1/status" -> jsonResponse(
                200,
                """{"protocol":1,"instance_id":"$INSTANCE_ID","name":"测试电脑"}""",
            )
            request.method == "GET" && path == "/api/v1/libraries" -> jsonResponse(
                200,
                """{"libraries":[{"id":"lib-1","name":"图库一","count":3},{"id":"lib-2","name":"图库二","count":1}]}""",
            )
            request.method == "DELETE" && path.startsWith("/api/v1/searches/") -> jsonResponse(200, "{}")
            request.method == "DELETE" && path.startsWith("/api/v1/query-images/") -> jsonResponse(200, "{}")
            else -> null
        }
    }

    private fun jsonResponse(code: Int, body: String): MockResponse =
        MockResponse().setResponseCode(code)
            .setHeader("Content-Type", "application/json")
            .setBody(body)

    private fun bytesResponse(bytes: ByteArray): MockResponse =
        MockResponse().setResponseCode(200)
            .setHeader("Content-Type", "image/jpeg")
            .setBody(Buffer().write(bytes))

    private fun errorBody(code: String, message: String): String =
        """{"error":{"code":"$code","message":"$message"}}"""

    private fun itemsJson(prefix: String, count: Int, startIndex: Int = 0): String =
        (0 until count).joinToString(",") { offset ->
            val index = startIndex + offset
            """{"media_id":"$prefix-$index","name":"$prefix-$index.jpg","width":640,"height":480,"tags":[],"library_id":"lib-1","library_name":"图库一","content_type":"image/jpeg","size_bytes":1024}"""
        }

    private fun pageBody(searchId: String, page: Int, total: Int, items: String): String =
        """{"search_id":"$searchId","page":$page,"page_size":100,"total":$total,"items":[$items]}"""

    private fun searchItem(mediaId: String, name: String, sizeBytes: Long): SearchItem = SearchItem(
        mediaId = mediaId,
        name = name,
        width = 800,
        height = 600,
        libraryId = "lib-1",
        libraryName = "图库一",
        contentType = "image/jpeg",
        sizeBytes = sizeBytes,
    )

    private fun writeTempFile(directoryName: String, bytes: ByteArray): File {
        val directory = File(
            targetContext.cacheDir,
            "zvec-test-source/$directoryName-${UUID.randomUUID()}",
        ).apply { mkdirs() }
        return File(directory, "query.jpg").apply { writeBytes(bytes) }
    }

    private fun newOutputFile(label: String): File {
        val directory = File(
            targetContext.cacheDir,
            "zvec-test-output/$label-${UUID.randomUUID()}",
        ).apply { mkdirs() }
        return File(directory, "$label.bin")
    }

    private fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    private companion object {
        const val INSTANCE_ID = "inst-test"
        const val SEARCH_ID_A = "search-A"
        const val SEARCH_ID_B = "search-B"
        const val SYNTHETIC_TOKEN = "synthetic-instrumented-test-token"

        val QUERY_IMAGE_BYTES = ByteArray(2048) { (it % 97).toByte() }
        val ORIGINAL_BYTES = ByteArray(4096) { (it % 251).toByte() }
    }
}
