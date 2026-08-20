package com.zvec.lanviewer.data.network

import android.content.Context
import android.net.TrafficStats
import android.os.Bundle
import android.os.Debug
import android.os.Process
import android.os.SystemClock
import androidx.test.platform.app.InstrumentationRegistry
import coil.EventListener
import coil.ImageLoader
import coil.annotation.ExperimentalCoilApi
import coil.decode.DataSource
import coil.disk.DiskCache
import coil.memory.MemoryCache
import coil.request.ImageRequest
import coil.request.SuccessResult
import coil.size.Dimension
import coil.size.Size
import com.zvec.lanviewer.ZvecApplication
import com.zvec.lanviewer.data.local.ConnectionReader
import com.zvec.lanviewer.data.model.RecommendationAction
import com.zvec.lanviewer.data.model.RecommendationActionRequest
import com.zvec.lanviewer.data.model.RecommendationItem
import com.zvec.lanviewer.data.model.RecommendationRequest
import com.zvec.lanviewer.data.model.RecommendationShownRequest
import com.zvec.lanviewer.data.model.RecommendationsResponse
import com.zvec.lanviewer.ui.RecommendationPreloadTask
import com.zvec.lanviewer.ui.startRecommendationPreload
import kotlinx.collections.immutable.toPersistentList
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.Call
import okhttp3.EventListener as OkHttpEventListener
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.io.IOException
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicIntegerArray
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.ceil

@OptIn(ExperimentalCoilApi::class)
class RecommendationNetworkBenchmarkTest {
    @Test
    fun benchmarkRecommendationNetworkPipeline() = runBlocking {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(
            "set -e $ARG_ENABLED true to run the real LAN benchmark",
            arguments.getString(ARG_ENABLED)?.toBooleanStrictOrNull() == true,
        )
        val application = instrumentation.targetContext.applicationContext as ZvecApplication
        val savedConnection = application.container.connectionStore.read()
        assumeTrue("benchmark requires an existing paired connection", savedConnection != null)
        assumeTrue("benchmark requires an existing encrypted bearer", application.container.repository.hasToken())
        val connection = requireNotNull(savedConnection)
        val savedOrigin = requireNotNull(connection.baseUrl.toHttpUrlOrNull())
        val requestedServer = arguments.getString(ARG_SERVER_URL)
        val requestedOrigin = if (requestedServer == null) {
            savedOrigin
        } else {
            requireNotNull(requestedServer.toHttpUrlOrNull()) { "benchmark server URL is invalid" }
        }
        require(sameOrigin(savedOrigin, requestedOrigin)) {
            "benchmark server must match the currently paired origin"
        }
        val concurrency = arguments.getString(ARG_CONCURRENCY)?.toIntOrNull() ?: 10
        val cycles = arguments.getString(ARG_BATCHES)?.toIntOrNull() ?: 20
        val idSeed = requireIdSeed(arguments.getString(ARG_ID_SEED))
        require(concurrency in 1..64) { "benchmark concurrency must be in 1..64" }
        require(cycles in 1..100) { "benchmark cycles must be in 1..100" }
        val ids = benchmarkIds(idSeed, cycles)
        val client = LanApiClient(
            connectionReader = ConnectionReader { connection },
            tokenStore = application.container.tokenStore,
            maxConcurrentRequests = concurrency,
        )
        val networkProbe = NetworkProbe()
        val coilRequestProbe = CoilRequestProbe()
        val instrumentedHttpClient = client.httpClient.newBuilder()
            .eventListenerFactory(networkProbe)
            .build()
        val cacheDirectory = File(
            application.cacheDir,
            "tc007-network-benchmark-${Process.myPid()}-${SystemClock.elapsedRealtimeNanos()}",
        )
        require(cacheDirectory.mkdirs()) { "unable to create owned benchmark cache" }
        val memoryCache = MemoryCache.Builder(application)
            .maxSizeBytes(TEST_MEMORY_CACHE_BYTES)
            .build()
        val diskCache = DiskCache.Builder()
            .directory(cacheDirectory)
            .maxSizeBytes(TEST_DISK_CACHE_BYTES)
            .build()
        val imageLoader = ImageLoader.Builder(application)
            .okHttpClient(instrumentedHttpClient)
            .memoryCache(memoryCache)
            .diskCache(diskCache)
            .respectCacheHeaders(true)
            .eventListenerFactory(coilRequestProbe)
            .build()
        val coilHarness = CoilHarness(
            context = application,
            imageLoader = imageLoader,
            allowedOrigin = savedOrigin,
            networkProbe = networkProbe,
        )
        val createProbe = CreateProbe()
        val shownProbe = ShownProbe()
        val metrics = BenchmarkMetrics(
            concurrency = concurrency,
            cycles = cycles,
            idSequenceSha256 = idSequenceSha256(ids),
        )
        val resourceSampler = ResourceSampler(this)
        val trafficBefore = TrafficStats.getUidRxBytes(Process.myUid())
        var failed = false
        var lastPreparedUrls = emptyList<String>()
        resourceSampler.start()
        try {
            repeat(cycles) { cycle ->
                val foreground = measureForeground(
                    scope = this,
                    client = client,
                    coilHarness = coilHarness,
                    createProbe = createProbe,
                    shownProbe = shownProbe,
                    ids = ids[cycle].foreground,
                    generation = cycle.toLong() * 2L,
                )
                metrics.addForeground(foreground)
                val prepared = measurePrepared(
                    scope = this,
                    client = client,
                    coilHarness = coilHarness,
                    createProbe = createProbe,
                    shownProbe = shownProbe,
                    ids = ids[cycle].prepared,
                    generation = cycle.toLong() * 2L + 1L,
                )
                metrics.addPrepared(prepared)
                lastPreparedUrls = prepared.urls
                if (!foreground.valid || !prepared.valid) failed = true
            }
            metrics.cancelProbe = runCancelProbe(
                scope = this,
                imageLoader = imageLoader,
                coilHarness = coilHarness,
                networkProbe = networkProbe,
                urls = lastPreparedUrls,
                dispatcher = instrumentedHttpClient.dispatcher,
            )
            if (!metrics.cancelProbe.valid) failed = true
        } catch (_: CancellationException) {
            metrics.cancellations += 1
            failed = true
        } catch (_: Throwable) {
            metrics.errors += 1
            failed = true
        } finally {
            metrics.resources = resourceSampler.stop()
            val trafficAfter = TrafficStats.getUidRxBytes(Process.myUid())
            metrics.trafficRxBytes = if (trafficBefore >= 0L && trafficAfter >= trafficBefore) {
                trafficAfter - trafficBefore
            } else {
                null
            }
            metrics.coil = CoilMetrics(
                sizePx = THUMBNAIL_SIZE_PX,
                networkCalls = networkProbe.networkCalls.get(),
                contentLengthBytes = networkProbe.contentLengthBytes.get(),
                unknownContentLengths = networkProbe.unknownContentLengths.get(),
                invalidContentLengths = networkProbe.invalidContentLengths.get(),
                cacheHitReloads = coilHarness.cacheHitReloads.get(),
                expectedCacheHitReloads = cycles * 2 * THUMBNAIL_COUNT,
                sameUrlSize = coilRequestProbe.sameUrlSize(),
            )
            metrics.duplicateCreateCount = createProbe.duplicateCreateCount()
            imageLoader.shutdown()
            if (!cacheDirectory.deleteRecursively()) {
                metrics.errors += 1
                failed = true
            }
            if (!metrics.contractValid()) {
                metrics.errors += 1
                failed = true
            }
            val json = metrics.toJson().toString()
            instrumentation.sendStatus(
                0,
                Bundle().apply { putString(RESULT_BUNDLE_KEY, json) },
            )
            println("$RESULT_PREFIX$json")
        }
        if (failed) fail("recommendation network benchmark failed its measured contract")
    }

    private suspend fun measureForeground(
        scope: CoroutineScope,
        client: LanApiClient,
        coilHarness: CoilHarness,
        createProbe: CreateProbe,
        shownProbe: ShownProbe,
        ids: RoleIds,
        generation: Long,
    ): RoleMetrics = coroutineScope {
        val run = startTrackedPreload(scope, client, coilHarness, createProbe, ids, generation)
        val status = async(start = CoroutineStart.UNDISPATCHED) {
            run.tracker.firstPreloadStarted.await()
            runCatching { timed { client.status() } }.getOrNull()
        }
        val response = run.task.awaitCritical()
        val committedItems = response.items.toPersistentList()
        check(committedItems.size == THUMBNAIL_COUNT)
        val t4Ms = run.tracker.fixedMilestoneMs(4)
        val shown = submitShown(client, shownProbe, response.batchId, ids.shownEventId)
        val action = shown?.let {
            runCatching {
                timed {
                    client.recordRecommendationAction(
                        response.batchId,
                        RecommendationActionRequest(
                            eventId = ids.actionEventId,
                            itemId = committedItems.first().itemId,
                            action = RecommendationAction.OPEN,
                        ),
                    )
                }
            }.getOrNull()
        }
        val outcome = run.task.awaitSettled()
        val t6Ms = run.tracker.fixedMilestoneMs(6)
        val t15Ms = run.tracker.fixedMilestoneMs(THUMBNAIL_COUNT)
        val cache = coilHarness.validateCacheReload(run.tracker.urls())
        val statusTiming = status.await()
        RoleMetrics(
            createMs = run.tracker.createMs,
            t4Ms = t4Ms,
            t6Ms = t6Ms,
            t15Ms = t15Ms,
            shownMs = shown?.elapsedMs,
            actionMs = action?.elapsedMs,
            statusMs = statusTiming?.elapsedMs,
            cacheHitReloads = cache.hits,
            cacheReloadNetworkCalls = cache.networkCalls,
            duplicatePreloads = run.tracker.duplicatePreloads(),
            preloadFailures = outcome.failedCount,
            urls = run.tracker.urls(),
        )
    }

    private suspend fun measurePrepared(
        scope: CoroutineScope,
        client: LanApiClient,
        coilHarness: CoilHarness,
        createProbe: CreateProbe,
        shownProbe: ShownProbe,
        ids: RoleIds,
        generation: Long,
    ): PreparedMetrics = coroutineScope {
        val run = startTrackedPreload(scope, client, coilHarness, createProbe, ids, generation)
        val status = async(start = CoroutineStart.UNDISPATCHED) {
            run.tracker.firstPreloadStarted.await()
            runCatching { timed { client.status() } }.getOrNull()
        }
        val outcome = run.task.awaitSettled()
        val shownBeforeConsume = shownProbe.count(run.tracker.batchId())
        val clickStartedAt = SystemClock.elapsedRealtimeNanos()
        val response = run.task.awaitCritical()
        val committedItems = response.items.toPersistentList()
        check(committedItems.size == THUMBNAIL_COUNT)
        val clickCommitMs = elapsedMs(clickStartedAt)
        val shown = submitShown(client, shownProbe, response.batchId, ids.shownEventId)
        val action = shown?.let {
            runCatching {
                timed {
                    client.recordRecommendationAction(
                        response.batchId,
                        RecommendationActionRequest(
                            eventId = ids.actionEventId,
                            itemId = committedItems.first().itemId,
                            action = RecommendationAction.OPEN,
                        ),
                    )
                }
            }.getOrNull()
        }
        val cache = coilHarness.validateCacheReload(run.tracker.urls())
        val statusTiming = status.await()
        PreparedMetrics(
            createMs = run.tracker.createMs,
            preloadT4Ms = run.tracker.fixedMilestoneMs(4),
            preloadT6Ms = run.tracker.fixedMilestoneMs(6),
            preloadT15Ms = run.tracker.fixedMilestoneMs(THUMBNAIL_COUNT),
            clickCommitMs = clickCommitMs,
            shownBeforeConsume = shownBeforeConsume,
            shownAfterConsume = shownProbe.count(response.batchId),
            shownMs = shown?.elapsedMs,
            actionMs = action?.elapsedMs,
            statusMs = statusTiming?.elapsedMs,
            cacheHitReloads = cache.hits,
            cacheReloadNetworkCalls = cache.networkCalls,
            duplicatePreloads = run.tracker.duplicatePreloads(),
            preloadFailures = outcome.failedCount,
            urls = run.tracker.urls(),
        )
    }

    private fun startTrackedPreload(
        scope: CoroutineScope,
        client: LanApiClient,
        coilHarness: CoilHarness,
        createProbe: CreateProbe,
        ids: RoleIds,
        generation: Long,
    ): TrackedPreload {
        val startedAtNanos = SystemClock.elapsedRealtimeNanos()
        val tracker = BatchPreloadTracker(startedAtNanos, coilHarness)
        val task = scope.startRecommendationPreload(
            requestId = ids.requestId,
            generation = generation,
            criticalCount = 4,
            create = { requestId ->
                createProbe.record(requestId)
                val created = timed {
                    client.recommendations(RecommendationRequest(requestId))
                }
                check(created.value.requestId == requestId)
                check(created.value.items.size == THUMBNAIL_COUNT)
                tracker.bind(created.value)
                tracker.createMs = created.elapsedMs
                created.value
            },
            thumbnailUrls = { response -> response.items.map(RecommendationItem::thumbnailUrl) },
            preload = tracker::preload,
        )
        return TrackedPreload(task, tracker)
    }

    private suspend fun runCancelProbe(
        scope: CoroutineScope,
        imageLoader: ImageLoader,
        coilHarness: CoilHarness,
        networkProbe: NetworkProbe,
        urls: List<String>,
        dispatcher: okhttp3.Dispatcher,
    ): CancelProbeMetrics {
        if (urls.size != THUMBNAIL_COUNT) return CancelProbeMetrics()
        imageLoader.memoryCache?.clear()
        imageLoader.diskCache?.clear()
        val nextNetworkCall = networkProbe.armNextCall()
        val cancelledBefore = networkProbe.cancelledCalls.get()
        val cancelAtNanos = AtomicLong(Long.MAX_VALUE)
        val lateCompletions = AtomicInteger()
        val job = scope.launch {
            coroutineScope {
                urls.map { url ->
                    async(start = CoroutineStart.UNDISPATCHED) {
                        val result = coilHarness.execute(url)
                        if (result.successful && SystemClock.elapsedRealtimeNanos() >= cancelAtNanos.get()) {
                            lateCompletions.incrementAndGet()
                        }
                    }
                }.awaitAll()
            }
        }
        val requested = withTimeoutOrNull(CANCEL_PROBE_START_TIMEOUT_MS) {
            nextNetworkCall.await()
            true
        } ?: false
        val cancelStartedAt = SystemClock.elapsedRealtimeNanos()
        cancelAtNanos.set(cancelStartedAt)
        job.cancelAndJoin()
        val latencyMs = elapsedMs(cancelStartedAt)
        withTimeoutOrNull(CANCEL_PROBE_IDLE_TIMEOUT_MS) {
            while (dispatcher.queuedCallsCount() > 0 || dispatcher.runningCallsCount() > 0) {
                delay(10)
            }
        }
        val queuedAfter = dispatcher.queuedCallsCount()
        val runningAfter = dispatcher.runningCallsCount()
        return CancelProbeMetrics(
            requested = requested,
            latencyMs = latencyMs,
            cancelledCalls = networkProbe.cancelledCalls.get() - cancelledBefore,
            lateCompletions = lateCompletions.get(),
            queuedAfter = queuedAfter,
            runningAfter = runningAfter,
            residualCalls = queuedAfter + runningAfter,
        )
    }

    private suspend fun <T> timed(block: suspend () -> T): Timed<T> {
        val startedAt = SystemClock.elapsedRealtimeNanos()
        return Timed(block(), elapsedMs(startedAt))
    }

    private suspend fun submitShown(
        client: LanApiClient,
        shownProbe: ShownProbe,
        batchId: String,
        eventId: String,
    ): Timed<Unit>? = runCatching {
        timed {
            client.markRecommendationsShown(batchId, RecommendationShownRequest(eventId))
        }.also { shownProbe.record(batchId) }
    }.getOrNull()

    private fun elapsedMs(startedAt: Long): Double =
        (SystemClock.elapsedRealtimeNanos() - startedAt) / 1_000_000.0

    private fun sameOrigin(left: HttpUrl, right: HttpUrl): Boolean =
        left.scheme == right.scheme && left.host == right.host && left.port == right.port

    @Test
    fun idSeedValidationFailsClosed() {
        listOf(null, "", "short", "contains space", "../unsafe", "x".repeat(129)).forEach { invalid ->
            assertTrue(runCatching { requireIdSeed(invalid) }.exceptionOrNull() is IllegalArgumentException)
        }
    }

    @Test
    fun idSequenceIsDeterministicAndSeparatedByCycleRoleAndKind() {
        val first = benchmarkIds("matrix-seed-20260803", 2)
        val second = benchmarkIds("matrix-seed-20260803", 2)

        assertEquals(first, second)
        assertNotEquals(first[0].foreground.requestId, first[0].foreground.shownEventId)
        assertNotEquals(first[0].foreground.requestId, first[0].prepared.requestId)
        assertNotEquals(first[0].prepared.requestId, first[1].prepared.requestId)
        assertEquals(idSequenceSha256(first), idSequenceSha256(second))
        assertEquals(FIXED_FIRST_REQUEST_ID, first[0].foreground.requestId)
        assertEquals(FIXED_SEQUENCE_SHA256, idSequenceSha256(first))
    }

    @Test
    fun twentyCyclesPlanFortyFreshRequestsAndExposeTheV2Contract() {
        val ids = benchmarkIds("matrix-seed-20260803", 20)
        val requestIds = ids.flatMap { cycle ->
            listOf(cycle.foreground.requestId, cycle.prepared.requestId)
        }
        val allIds = ids.flatMap { cycle ->
            listOf(cycle.foreground, cycle.prepared).flatMap { role ->
                listOf(role.requestId, role.shownEventId, role.actionEventId)
            }
        }
        val json = BenchmarkMetrics(
            concurrency = 10,
            cycles = 20,
            idSequenceSha256 = idSequenceSha256(ids),
        ).toJson()

        assertEquals(40, requestIds.size)
        assertEquals(40, requestIds.toSet().size)
        assertEquals(120, allIds.toSet().size)
        assertEquals("tc007-android-network-v2", json.getString("schema"))
        assertTrue(json.getJSONObject("coil").has("same_url_size"))
        assertTrue(json.getJSONObject("resources").has("cpu_peak_core_percent"))
        assertTrue(json.getJSONObject("prepared").has("prepared_click_commit_ms"))
        assertTrue(json.getJSONObject("cancel_probe").has("residual_calls"))
    }

    private fun requireIdSeed(raw: String?): String = requireNotNull(
        raw?.takeIf { it.matches(Regex("[A-Za-z0-9._-]{8,128}")) },
    ) { "tc007IdSeed is required and must use 8..128 safe characters" }

    private fun benchmarkIds(seed: String, cycles: Int): List<CycleIds> =
        List(cycles) { cycle ->
            CycleIds(
                foreground = roleIds(seed, cycle, "foreground"),
                prepared = roleIds(seed, cycle, "prepared"),
            )
        }

    private fun roleIds(seed: String, cycle: Int, role: String): RoleIds = RoleIds(
        requestId = deterministicId(seed, cycle, "$role.request"),
        shownEventId = deterministicId(seed, cycle, "$role.shown"),
        actionEventId = deterministicId(seed, cycle, "$role.action"),
    )

    private fun deterministicId(seed: String, cycle: Int, kind: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(
            "tc007-id-v1\u0000$seed\u0000$cycle\u0000$kind".toByteArray(Charsets.UTF_8),
        )
        val uuidBytes = digest.copyOf(16)
        uuidBytes[6] = ((uuidBytes[6].toInt() and 0x0f) or 0x50).toByte()
        uuidBytes[8] = ((uuidBytes[8].toInt() and 0x3f) or 0x80).toByte()
        val buffer = ByteBuffer.wrap(uuidBytes)
        return UUID(buffer.long, buffer.long).toString()
    }

    private fun idSequenceSha256(ids: List<CycleIds>): String {
        val digest = MessageDigest.getInstance("SHA-256")
        ids.forEach { cycle ->
            listOf(cycle.foreground, cycle.prepared).forEach { role ->
                listOf(role.requestId, role.shownEventId, role.actionEventId).forEach { id ->
                    digest.update(id.toByteArray(Charsets.UTF_8))
                    digest.update(0.toByte())
                }
            }
        }
        return digest.digest().joinToString("") { byte -> "%02x".format(byte) }
    }

    private class CoilHarness(
        private val context: Context,
        private val imageLoader: ImageLoader,
        private val allowedOrigin: HttpUrl,
        private val networkProbe: NetworkProbe,
    ) {
        val cacheHitReloads = AtomicInteger()

        suspend fun execute(url: String): CoilLoadResult {
            val parsed = url.toHttpUrlOrNull()
            if (parsed == null || parsed.scheme != allowedOrigin.scheme || parsed.host != allowedOrigin.host ||
                parsed.port != allowedOrigin.port
            ) return CoilLoadResult(error = true)
            return try {
                val result = imageLoader.execute(request(url))
                if (result is SuccessResult) {
                    CoilLoadResult(dataSource = result.dataSource)
                } else {
                    CoilLoadResult(error = true)
                }
            } catch (error: CancellationException) {
                throw error
            } catch (_: Throwable) {
                CoilLoadResult(error = true)
            }
        }

        suspend fun validateCacheReload(urls: List<String>): CacheReloadMetrics = coroutineScope {
            val networkBefore = networkProbe.networkCalls.get()
            val results = urls.map { url -> async { execute(url) } }.awaitAll()
            val hits = results.count {
                it.dataSource == DataSource.MEMORY_CACHE || it.dataSource == DataSource.DISK
            }
            cacheHitReloads.addAndGet(hits)
            CacheReloadMetrics(
                hits = hits,
                networkCalls = networkProbe.networkCalls.get() - networkBefore,
            )
        }

        private fun request(url: String): ImageRequest = ImageRequest.Builder(context)
            .data(url)
            .size(THUMBNAIL_SIZE_PX)
            .build()
    }

    private class BatchPreloadTracker(
        private val startedAtNanos: Long,
        private val coilHarness: CoilHarness,
    ) {
        private val bound = CompletableDeferred<Unit>()
        val firstPreloadStarted = CompletableDeferred<Unit>()
        private val completions = List(THUMBNAIL_COUNT) { CompletableDeferred<Long>() }
        private val preloadCounts = AtomicIntegerArray(THUMBNAIL_COUNT)
        private lateinit var thumbnailUrls: List<String>
        private lateinit var indicesByUrl: Map<String, Int>
        private lateinit var recommendationBatchId: String
        var createMs: Double = 0.0

        fun bind(response: RecommendationsResponse) {
            recommendationBatchId = response.batchId
            thumbnailUrls = response.items.map(RecommendationItem::thumbnailUrl)
            check(thumbnailUrls.size == THUMBNAIL_COUNT)
            check(thumbnailUrls.toSet().size == THUMBNAIL_COUNT)
            indicesByUrl = thumbnailUrls.withIndex().associate { (index, url) -> url to index }
            bound.complete(Unit)
        }

        suspend fun preload(url: String): Boolean {
            bound.await()
            val index = indicesByUrl[url] ?: return false
            preloadCounts.incrementAndGet(index)
            firstPreloadStarted.complete(Unit)
            return try {
                val result = coilHarness.execute(url)
                completions[index].complete(SystemClock.elapsedRealtimeNanos())
                result.successful
            } catch (error: CancellationException) {
                completions[index].cancel(error)
                throw error
            }
        }

        suspend fun fixedMilestoneMs(count: Int): Double {
            bound.await()
            val completedAt = completions.take(count).awaitAll().maxOrNull() ?: startedAtNanos
            return (completedAt - startedAtNanos) / 1_000_000.0
        }

        suspend fun urls(): List<String> {
            bound.await()
            return thumbnailUrls
        }

        suspend fun batchId(): String {
            bound.await()
            return recommendationBatchId
        }

        fun duplicatePreloads(): Int = (0 until THUMBNAIL_COUNT).sumOf { index ->
            (preloadCounts.get(index) - 1).coerceAtLeast(0)
        }
    }

    private class NetworkProbe : OkHttpEventListener.Factory {
        val networkCalls = AtomicInteger()
        val contentLengthBytes = AtomicLong()
        val unknownContentLengths = AtomicInteger()
        val invalidContentLengths = AtomicInteger()
        val cancelledCalls = AtomicInteger()
        private val nextCallLock = Any()
        private var nextCall = CompletableDeferred<Unit>()

        fun armNextCall(): CompletableDeferred<Unit> = synchronized(nextCallLock) {
            CompletableDeferred<Unit>().also { nextCall = it }
        }

        override fun create(call: Call): OkHttpEventListener = object : OkHttpEventListener() {
            override fun callStart(call: Call) {
                networkCalls.incrementAndGet()
                synchronized(nextCallLock) { nextCall.complete(Unit) }
            }

            override fun responseHeadersEnd(call: Call, response: Response) {
                val lengthHeader = response.header("Content-Length")
                val length = lengthHeader?.toLongOrNull()
                when {
                    lengthHeader == null -> unknownContentLengths.incrementAndGet()
                    length == null || length < 0L -> invalidContentLengths.incrementAndGet()
                    length > MAX_THUMBNAIL_BYTES -> invalidContentLengths.incrementAndGet()
                    else -> contentLengthBytes.addAndGet(length)
                }
            }

            override fun canceled(call: Call) {
                cancelledCalls.incrementAndGet()
            }
        }
    }

    private class CoilRequestProbe : EventListener.Factory {
        private val invalidRequests = AtomicInteger()
        private val startsByUrl = ConcurrentHashMap<String, AtomicInteger>()

        override fun create(request: ImageRequest): EventListener = object : EventListener {
            override fun onStart(request: ImageRequest) {
                val url = request.data as? String
                if (url == null) {
                    invalidRequests.incrementAndGet()
                } else {
                    startsByUrl.computeIfAbsent(url) { AtomicInteger() }.incrementAndGet()
                }
            }

            override fun resolveSizeEnd(request: ImageRequest, size: Size) {
                val width = (size.width as? Dimension.Pixels)?.px
                val height = (size.height as? Dimension.Pixels)?.px
                if (width != THUMBNAIL_SIZE_PX || height != THUMBNAIL_SIZE_PX) {
                    invalidRequests.incrementAndGet()
                }
            }
        }

        fun sameUrlSize(): Boolean = invalidRequests.get() == 0 && startsByUrl.isNotEmpty()
    }

    private class CreateProbe {
        private val creates = ConcurrentHashMap<String, AtomicInteger>()

        fun record(requestId: String) {
            creates.computeIfAbsent(requestId) { AtomicInteger() }.incrementAndGet()
        }

        fun duplicateCreateCount(): Int = creates.values.sumOf { count ->
            (count.get() - 1).coerceAtLeast(0)
        }
    }

    private class ShownProbe {
        private val shownByBatch = ConcurrentHashMap<String, AtomicInteger>()

        fun record(batchId: String) {
            shownByBatch.computeIfAbsent(batchId) { AtomicInteger() }.incrementAndGet()
        }

        fun count(batchId: String): Int = shownByBatch[batchId]?.get() ?: 0
    }

    private class ResourceSampler(private val scope: CoroutineScope) {
        private val cpuStartMs = Process.getElapsedCpuTime()
        private val wallStartMs = SystemClock.elapsedRealtime()
        private val pssBeforeKb = Debug.getPss()
        private val pssPeakKb = AtomicLong(pssBeforeKb)
        private val cpuPeakPercent = AtomicLong()
        private var job: Job? = null

        fun start() {
            check(job == null)
            job = scope.launch(Dispatchers.Default) {
                var previousCpuMs = Process.getElapsedCpuTime()
                var previousWallMs = SystemClock.elapsedRealtime()
                while (currentCoroutineContext().isActive) {
                    delay(RESOURCE_SAMPLE_INTERVAL_MS)
                    val cpuNow = Process.getElapsedCpuTime()
                    val wallNow = SystemClock.elapsedRealtime()
                    val wallDelta = (wallNow - previousWallMs).coerceAtLeast(1L)
                    val intervalPercent = (cpuNow - previousCpuMs).coerceAtLeast(0L) * 100_000L / wallDelta
                    cpuPeakPercent.updateAndGet { previous -> maxOf(previous, intervalPercent) }
                    pssPeakKb.updateAndGet { previous -> maxOf(previous, Debug.getPss()) }
                    previousCpuMs = cpuNow
                    previousWallMs = wallNow
                }
            }
        }

        suspend fun stop(): ResourceMetrics {
            job?.cancelAndJoin()
            pssPeakKb.updateAndGet { previous -> maxOf(previous, Debug.getPss()) }
            val wallMs = (SystemClock.elapsedRealtime() - wallStartMs).coerceAtLeast(1L)
            val cpuMs = (Process.getElapsedCpuTime() - cpuStartMs).coerceAtLeast(0L)
            return ResourceMetrics(
                cpuAvgCorePercent = cpuMs * 100.0 / wallMs,
                cpuPeakCorePercent = cpuPeakPercent.get() / 1_000.0,
                pssBeforeKb = pssBeforeKb,
                pssAfterKb = Debug.getPss(),
                pssPeakKb = pssPeakKb.get(),
            )
        }
    }

    private data class Timed<T>(val value: T, val elapsedMs: Double)

    private data class CycleIds(val foreground: RoleIds, val prepared: RoleIds)

    private data class RoleIds(
        val requestId: String,
        val shownEventId: String,
        val actionEventId: String,
    )

    private data class TrackedPreload(
        val task: RecommendationPreloadTask<RecommendationsResponse>,
        val tracker: BatchPreloadTracker,
    )

    private data class CoilLoadResult(
        val dataSource: DataSource? = null,
        val error: Boolean = false,
    ) {
        val successful: Boolean get() = !error && dataSource != null
    }

    private data class CacheReloadMetrics(val hits: Int, val networkCalls: Int)

    private data class RoleMetrics(
        val createMs: Double,
        val t4Ms: Double,
        val t6Ms: Double,
        val t15Ms: Double,
        val shownMs: Double?,
        val actionMs: Double?,
        val statusMs: Double?,
        val cacheHitReloads: Int,
        val cacheReloadNetworkCalls: Int,
        val duplicatePreloads: Int,
        val preloadFailures: Int,
        val urls: List<String>,
    ) {
        val valid: Boolean get() = shownMs != null && actionMs != null && statusMs != null &&
            cacheHitReloads == THUMBNAIL_COUNT && cacheReloadNetworkCalls == 0 &&
            duplicatePreloads == 0 && preloadFailures == 0
    }

    private data class PreparedMetrics(
        val createMs: Double,
        val preloadT4Ms: Double,
        val preloadT6Ms: Double,
        val preloadT15Ms: Double,
        val clickCommitMs: Double,
        val shownBeforeConsume: Int,
        val shownAfterConsume: Int,
        val shownMs: Double?,
        val actionMs: Double?,
        val statusMs: Double?,
        val cacheHitReloads: Int,
        val cacheReloadNetworkCalls: Int,
        val duplicatePreloads: Int,
        val preloadFailures: Int,
        val urls: List<String>,
    ) {
        val valid: Boolean get() = shownBeforeConsume == 0 && shownAfterConsume == 1 &&
            shownMs != null && actionMs != null && statusMs != null &&
            cacheHitReloads == THUMBNAIL_COUNT && cacheReloadNetworkCalls == 0 &&
            duplicatePreloads == 0 && preloadFailures == 0
    }

    private data class ResourceMetrics(
        val cpuAvgCorePercent: Double = 0.0,
        val cpuPeakCorePercent: Double = 0.0,
        val pssBeforeKb: Long = 0L,
        val pssAfterKb: Long = 0L,
        val pssPeakKb: Long = 0L,
    )

    private data class CoilMetrics(
        val sizePx: Int = THUMBNAIL_SIZE_PX,
        val networkCalls: Int = 0,
        val contentLengthBytes: Long = 0L,
        val unknownContentLengths: Int = 0,
        val invalidContentLengths: Int = 0,
        val cacheHitReloads: Int = 0,
        val expectedCacheHitReloads: Int = 0,
        val sameUrlSize: Boolean = false,
    )

    private data class CancelProbeMetrics(
        val requested: Boolean = false,
        val latencyMs: Double = 0.0,
        val cancelledCalls: Int = 0,
        val lateCompletions: Int = 0,
        val queuedAfter: Int = -1,
        val runningAfter: Int = -1,
        val residualCalls: Int = -1,
    ) {
        val valid: Boolean get() = requested && cancelledCalls > 0 && lateCompletions == 0 &&
            queuedAfter == 0 && runningAfter == 0 && residualCalls == 0
    }

    private class BenchmarkMetrics(
        private val concurrency: Int,
        private val cycles: Int,
        private val idSequenceSha256: String,
    ) {
        private val foregroundCreateMs = mutableListOf<Double>()
        private val foregroundT4Ms = mutableListOf<Double>()
        private val foregroundT6Ms = mutableListOf<Double>()
        private val foregroundT15Ms = mutableListOf<Double>()
        private val preparedCreateMs = mutableListOf<Double>()
        private val preparedPreloadT4Ms = mutableListOf<Double>()
        private val preparedPreloadT6Ms = mutableListOf<Double>()
        private val preparedPreloadT15Ms = mutableListOf<Double>()
        private val preparedClickCommitMs = mutableListOf<Double>()
        private val shownMs = mutableListOf<Double>()
        private val actionMs = mutableListOf<Double>()
        private val statusMs = mutableListOf<Double>()
        private var preparedCreated = 0
        private var preparedShownBeforeConsume = 0
        private var preparedShownAfterConsume = 0
        var duplicateCreateCount = 0
        var trafficRxBytes: Long? = null
        var errors = 0
        var cancellations = 0
        var resources = ResourceMetrics()
        var coil = CoilMetrics()
        var cancelProbe = CancelProbeMetrics()

        fun addForeground(batch: RoleMetrics) {
            foregroundCreateMs += batch.createMs
            foregroundT4Ms += batch.t4Ms
            foregroundT6Ms += batch.t6Ms
            foregroundT15Ms += batch.t15Ms
            batch.shownMs?.let(shownMs::add)
            batch.actionMs?.let(actionMs::add)
            batch.statusMs?.let(statusMs::add)
            if (!batch.valid) errors += 1
        }

        fun addPrepared(batch: PreparedMetrics) {
            preparedCreated += 1
            preparedCreateMs += batch.createMs
            preparedPreloadT4Ms += batch.preloadT4Ms
            preparedPreloadT6Ms += batch.preloadT6Ms
            preparedPreloadT15Ms += batch.preloadT15Ms
            preparedClickCommitMs += batch.clickCommitMs
            preparedShownBeforeConsume += batch.shownBeforeConsume
            preparedShownAfterConsume += batch.shownAfterConsume
            batch.shownMs?.let(shownMs::add)
            batch.actionMs?.let(actionMs::add)
            batch.statusMs?.let(statusMs::add)
            if (!batch.valid) errors += 1
        }

        fun contractValid(): Boolean = errors == 0 && cancellations == 0 &&
            foregroundT15Ms.size == cycles && preparedCreated == cycles &&
            preparedShownBeforeConsume == 0 && preparedShownAfterConsume == cycles &&
            duplicateCreateCount == 0 &&
            coil.cacheHitReloads == coil.expectedCacheHitReloads && coil.sameUrlSize &&
            coil.unknownContentLengths == 0 && coil.invalidContentLengths == 0 &&
            cancelProbe.valid

        fun toJson(): JSONObject = JSONObject()
            .put("schema", "tc007-android-network-v2")
            .put("client_concurrency", concurrency)
            .put("requested_cycles", cycles)
            .put("completed_foreground", foregroundT15Ms.size)
            .put("planned_new_requests", cycles * 2)
            .put("id_sequence_sha256", idSequenceSha256)
            .put("timing_ms", JSONObject().apply {
                putSamples("foreground_create", foregroundCreateMs)
                putSamples("foreground_t4", foregroundT4Ms)
                putSamples("foreground_t6", foregroundT6Ms)
                putSamples("foreground_t15", foregroundT15Ms)
                putSamples("prepared_create", preparedCreateMs)
                putSamples("prepared_preload_t4", preparedPreloadT4Ms)
                putSamples("prepared_preload_t6", preparedPreloadT6Ms)
                putSamples("prepared_preload_t15", preparedPreloadT15Ms)
                putSamples("prepared_click_commit", preparedClickCommitMs)
                putSamples("shown", shownMs)
                putSamples("action", actionMs)
                putSamples("status_under_contention", statusMs)
            })
            .put("coil", JSONObject()
                .put("size_px", coil.sizePx)
                .put("network_calls", coil.networkCalls)
                .put("content_length_bytes", coil.contentLengthBytes)
                .put("unknown_content_lengths", coil.unknownContentLengths)
                .put("invalid_content_lengths", coil.invalidContentLengths)
                .put("cache_hit_reloads", coil.cacheHitReloads)
                .put("same_url_size", coil.sameUrlSize))
            .put("resources", JSONObject()
                .put("cpu_avg_core_percent", resources.cpuAvgCorePercent)
                .put("cpu_peak_core_percent", resources.cpuPeakCorePercent)
                .put("pss_before_kb", resources.pssBeforeKb)
                .put("pss_after_kb", resources.pssAfterKb)
                .put("pss_peak_kb", resources.pssPeakKb))
            .put("prepared", JSONObject()
                .put("scope", "production_preloader_state_materialization_no_compose_paint")
                .put("created", preparedCreated)
                .put("shown_before_consume", preparedShownBeforeConsume)
                .put("duplicate_create_count", duplicateCreateCount)
                .put("prepared_click_commit_ms", samplesJson(preparedClickCommitMs))
                .put("shown_after_consume", preparedShownAfterConsume))
            .put("cancel_probe", JSONObject()
                .put("scope", "test_owned_coil_preload_job")
                .put("requested", cancelProbe.requested)
                .put("latency_ms", cancelProbe.latencyMs)
                .put("cancelled_calls", cancelProbe.cancelledCalls)
                .put("late_completions", cancelProbe.lateCompletions)
                .put("queued_after", cancelProbe.queuedAfter)
                .put("running_after", cancelProbe.runningAfter)
                .put("residual_calls", cancelProbe.residualCalls))
            .put("traffic_rx_bytes", trafficRxBytes ?: JSONObject.NULL)
            .put("errors", errors)
            .put("cancellations", cancellations)

        private fun JSONObject.putSamples(name: String, values: List<Double>) {
            put(name, samplesJson(values))
        }

        private fun samplesJson(values: List<Double>): JSONObject = JSONObject()
            .put("p50", percentile(values, 0.50))
            .put("p95", percentile(values, 0.95))
            .put("samples", JSONArray(values))

        private fun percentile(values: List<Double>, quantile: Double): Any {
            if (values.isEmpty()) return JSONObject.NULL
            val sorted = values.sorted()
            val index = (ceil(sorted.size * quantile).toInt() - 1).coerceIn(sorted.indices)
            return sorted[index]
        }
    }

    companion object {
        private const val ARG_ENABLED = "tc007NetworkBenchmark"
        private const val ARG_CONCURRENCY = "tc007ClientConcurrency"
        private const val ARG_SERVER_URL = "tc007ServerUrl"
        private const val ARG_BATCHES = "tc007BatchCount"
        private const val ARG_ID_SEED = "tc007IdSeed"
        private const val RESULT_BUNDLE_KEY = "tc007_network_benchmark_json"
        private const val RESULT_PREFIX = "TC007_RECOMMENDATION_NETWORK_BENCHMARK="
        private const val THUMBNAIL_COUNT = 15
        private const val THUMBNAIL_SIZE_PX = 640
        private const val RESOURCE_SAMPLE_INTERVAL_MS = 100L
        private const val MAX_THUMBNAIL_BYTES = 16L * 1024L * 1024L
        private const val TEST_MEMORY_CACHE_BYTES = 64 * 1024 * 1024
        private const val TEST_DISK_CACHE_BYTES = 512L * 1024L * 1024L
        private const val CANCEL_PROBE_START_TIMEOUT_MS = 5_000L
        private const val CANCEL_PROBE_IDLE_TIMEOUT_MS = 5_000L
        private const val FIXED_FIRST_REQUEST_ID = "b240bc7b-35a3-53c6-a33f-a48b2ca7bac8"
        private const val FIXED_SEQUENCE_SHA256 = "ced2815951e4f4f6954281d137d9cba79672dab1967bdd9f1df169cb61bcebde"
    }
}
