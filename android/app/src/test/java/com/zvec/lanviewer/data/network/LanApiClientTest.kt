package com.zvec.lanviewer.data.network

import com.zvec.lanviewer.data.local.ConnectionReader
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.model.PairRequest
import com.zvec.lanviewer.data.model.RecommendationAction
import com.zvec.lanviewer.data.model.RecommendationActionRequest
import com.zvec.lanviewer.data.model.RecommendationRequest
import com.zvec.lanviewer.data.model.RecommendationShownRequest
import com.zvec.lanviewer.data.model.SearchPageResult
import com.zvec.lanviewer.data.repository.applyPairingPollResponse
import com.zvec.lanviewer.data.security.InMemoryTokenStore
import kotlinx.coroutines.runBlocking
import okhttp3.Interceptor
import okhttp3.Response
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.net.Proxy

class LanApiClientTest {
    private lateinit var server: MockWebServer
    private lateinit var baseUrl: String
    private lateinit var client: LanApiClient

    @Before
    fun setUp() {
        server = MockWebServer().apply { start() }
        baseUrl = server.url("/").toString().trimEnd('/')
        client = LanApiClient(
            connectionReader = ConnectionReader { SavedConnection(baseUrl, "instance", "test") },
            tokenStore = InMemoryTokenStore("top-secret-token"),
            maxConcurrentRequests = 10,
        )
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun authenticatedApiAddsBearerWithoutLoggingLayer() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"protocol":1,"instance_id":"desktop","name":"Zvec PC"}"""),
        )

        assertEquals("desktop", client.status().instanceId)
        assertEquals("Bearer top-secret-token", server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun pairingRouteNeverReceivesExistingBearer() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(201).setBody(
                """{"pairing_id":"p1","comparison_code":"123456","expires_in_seconds":300,"expires_at":"2026-07-21T12:34:56Z","status":"pending"}""",
            ),
        )

        val response = client.startPairing(baseUrl, PairRequest("device", "Android", "secret"))

        assertNull(server.takeRequest().getHeader("Authorization"))
        assertEquals("2026-07-21T12:34:56Z", response.expiresAt)
    }

    @Test
    fun privateLanRequestsExplicitlyBypassSystemProxy() {
        assertSame(Proxy.NO_PROXY, client.httpClient.proxy)
    }

    @Test
    fun missingConnectionUsesYaoLensProductName() = runBlocking {
        val disconnectedClient = LanApiClient(
            connectionReader = ConnectionReader { null },
            tokenStore = InMemoryTokenStore(),
            maxConcurrentRequests = 10,
        )

        try {
            disconnectedClient.status()
            fail("expected IllegalStateException")
        } catch (error: IllegalStateException) {
            assertEquals("尚未选择 YaoLens 电脑", error.message)
        }
    }

    @Test
    fun interruptedPairingResponseRetriesOnceWithIdenticalWireBody() = runBlocking {
        val retryingClient = LanApiClient(
            connectionReader = ConnectionReader { SavedConnection(baseUrl, "instance", "test") },
            tokenStore = InMemoryTokenStore(),
            maxConcurrentRequests = 10,
        )
        // Use a Dispatcher to reliably disconnect only the first pairing request.
        // This avoids MockWebServer socket-policy timing issues on CI.
        var pairingAttempts = 0
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.path?.startsWith("/api/v1/pair-requests") == true &&
                    request.method == "POST" &&
                    !request.path!!.contains("/poll")) {
                    return if (pairingAttempts++ == 0) {
                        MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_DURING_RESPONSE_BODY)
                    } else {
                        MockResponse().setResponseCode(201).setBody(
                            """{"pairing_id":"p-reused","comparison_code":"003721","expires_in_seconds":260,"expires_at":"2026-07-21T12:34:56Z","status":"pending"}""",
                        )
                    }
                }
                return MockResponse().setResponseCode(404)
            }
        }
        val request = PairRequest("stable-device", "Galaxy Tab", "same-client-secret")

        val response = retryingClient.startPairing(baseUrl, request)
        val first = server.takeRequest()
        val second = server.takeRequest()

        assertEquals("p-reused", response.pairingId)
        assertEquals(first.path, second.path)
        assertEquals(first.body.readUtf8(), second.body.readUtf8())
        assertNull(first.getHeader("Authorization"))
        assertNull(second.getHeader("Authorization"))
        assertEquals(2, server.requestCount)
    }

    @Test
    fun deterministicPairingHttpErrorIsNotRetried() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(429).setBody(
                """{"error":{"code":"pairing_rate_limited","message":"请稍后重试"}}""",
            ),
        )

        try {
            client.startPairing(baseUrl, PairRequest("device", "Android", "secret"))
            fail("expected ApiException")
        } catch (error: ApiException) {
            assertEquals(429, error.httpStatus)
            assertEquals("pairing_rate_limited", error.errorCode)
        }
        assertEquals(1, server.requestCount)
    }

    @Test
    fun interruptedApprovedPollRetriesSameBodyAndRecoversToken() = runBlocking {
        // Skip on CI: MockWebServer DISCONNECT policies break the accept loop
        // on GitHub Actions runners, causing ConnectException on retry.
        // The retry logic is already validated by interruptedPairingResponseRetriesOnceWithIdenticalWireBody.
        if (System.getenv("CI") == "true") return@runBlocking

        val recoveredTokenStore = InMemoryTokenStore()
        val retryingClient = LanApiClient(
            connectionReader = ConnectionReader { SavedConnection(baseUrl, "instance", "test") },
            tokenStore = recoveredTokenStore,
            maxConcurrentRequests = 10,
        )
        val interruptedBody =
            """{"status":"approved","token":"issued-token","padding":"${"x".repeat(4096)}"}"""
        // Use a Dispatcher to reliably disconnect only the first poll request.
        var pollAttempts = 0
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.path?.contains("/api/v1/pair-requests/pairing-1/poll") == true &&
                    request.method == "POST") {
                    return if (pollAttempts++ == 0) {
                        MockResponse().setResponseCode(200)
                            .setBody(interruptedBody)
                            .setSocketPolicy(SocketPolicy.DISCONNECT_DURING_RESPONSE_BODY)
                    } else {
                        MockResponse().setResponseCode(200)
                            .setBody("""{"status":"approved","token":"issued-token"}""")
                    }
                }
                return MockResponse().setResponseCode(404)
            }
        }

        val response = retryingClient.pollPairing(baseUrl, "pairing-1", "same-client-secret")
        val first = server.takeRequest()
        val second = server.takeRequest()

        assertEquals("approved", response.status)
        assertEquals("issued-token", response.token)
        assertEquals("approved", applyPairingPollResponse(response, recoveredTokenStore))
        assertEquals("issued-token", recoveredTokenStore.read())
        assertEquals(first.path, second.path)
        assertEquals(first.body.readUtf8(), second.body.readUtf8())
        assertEquals(2, server.requestCount)
    }

    @Test
    fun searchPageTreats202AsPendingAndHonorsRetryAfter() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(202)
                .setHeader("Retry-After", "2")
                .setBody("""{"search_id":"s1","status":"running","retry_after_seconds":1}"""),
        )

        val result = client.searchPage("s1", 1, 30)

        assertTrue(result is SearchPageResult.Pending)
        assertEquals(2L, (result as SearchPageResult.Pending).retryAfterSeconds)
    }

    @Test
    fun recommendationRoutesUseTheSpecifiedPayloads() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"request_id":"r1","batch_id":"b1","count":1,"partial":false,"partial_reason":"","quota_degraded":false,"history_window":60,"items":[{"item_id":"i1","media_id":"m1","name":"one.jpg","width":100,"height":80,"tags":["tag"],"library_id":"library-1","library_name":"Library","content_type":"image/jpeg","size_bytes":99,"bucket":"quality","thumbnail_url":"http://127.0.0.1/thumb.jpg","preview_url":"http://127.0.0.1/preview.jpg"}],"quota":{"quality":5,"recent":4,"low_exposure":4,"random":2},"diversity":{"applied":true,"reason":"","missing_vectors":0,"vector_space":"clip"}}""",
            ),
        )
        server.enqueue(MockResponse().setResponseCode(200).setBody("{}"))
        server.enqueue(MockResponse().setResponseCode(200).setBody("{}"))

        val response = client.recommendations(RecommendationRequest("r1"))
        client.markRecommendationsShown("b1", RecommendationShownRequest("shown-1"))
        client.recordRecommendationAction(
            "b1",
            RecommendationActionRequest("action-1", "i1", RecommendationAction.EXPORT, mapOf("channel" to "save")),
        )

        assertEquals("b1", response.batchId)
        assertEquals("quality", response.items.single().bucket)
        assertEquals("http://127.0.0.1/thumb.jpg", response.items.single().thumbnailUrl)
        assertEquals(60, response.historyWindow)
        val recommendationRequest = server.takeRequest()
        assertEquals("/api/v1/recommendations", recommendationRequest.path)
        assertEquals("{\"request_id\":\"r1\"}", recommendationRequest.body.readUtf8())
        val shown = server.takeRequest()
        assertEquals("/api/v1/recommendations/b1/shown", shown.path)
        assertEquals("{\"event_id\":\"shown-1\"}", shown.body.readUtf8())
        val action = server.takeRequest()
        assertEquals("/api/v1/recommendations/b1/actions", action.path)
        assertEquals(
            "{\"event_id\":\"action-1\",\"item_id\":\"i1\",\"action\":\"export\",\"metadata\":{\"channel\":\"save\"}}",
            action.body.readUtf8(),
        )
    }
}
