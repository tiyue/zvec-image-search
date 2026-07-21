package com.zvec.lanviewer.data.network

import com.zvec.lanviewer.data.local.ConnectionReader
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.model.PairRequest
import com.zvec.lanviewer.data.model.SearchPageResult
import com.zvec.lanviewer.data.repository.applyPairingPollResponse
import com.zvec.lanviewer.data.security.InMemoryTokenStore
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
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
    fun interruptedPairingResponseRetriesOnceWithIdenticalWireBody() = runBlocking {
        val retryingClient = LanApiClient(
            connectionReader = ConnectionReader { SavedConnection(baseUrl, "instance", "test") },
            tokenStore = InMemoryTokenStore(),
            maxConcurrentRequests = 10,
        )
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        server.enqueue(
            MockResponse().setResponseCode(201).setBody(
                """{"pairing_id":"p-reused","comparison_code":"003721","expires_in_seconds":260,"expires_at":"2026-07-21T12:34:56Z","status":"pending"}""",
            ),
        )
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
        val recoveredTokenStore = InMemoryTokenStore()
        val retryingClient = LanApiClient(
            connectionReader = ConnectionReader { SavedConnection(baseUrl, "instance", "test") },
            tokenStore = recoveredTokenStore,
            maxConcurrentRequests = 10,
        )
        val interruptedBody =
            """{"status":"approved","token":"issued-token","padding":"${"x".repeat(4096)}"}"""
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody(interruptedBody)
                .setSocketPolicy(SocketPolicy.DISCONNECT_DURING_RESPONSE_BODY),
        )
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody("""{"status":"approved","token":"issued-token"}"""),
        )

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
}
