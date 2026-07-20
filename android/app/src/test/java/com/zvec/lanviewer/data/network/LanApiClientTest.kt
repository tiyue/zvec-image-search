package com.zvec.lanviewer.data.network

import com.zvec.lanviewer.data.local.ConnectionReader
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.model.PairRequest
import com.zvec.lanviewer.data.model.SearchPageResult
import com.zvec.lanviewer.data.security.InMemoryTokenStore
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

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
                """{"pairing_id":"p1","comparison_code":"123456","expires_in_seconds":300,"status":"pending"}""",
            ),
        )

        client.startPairing(baseUrl, PairRequest("device", "Android", "secret"))

        assertNull(server.takeRequest().getHeader("Authorization"))
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
