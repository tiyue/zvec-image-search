package com.zvec.lanviewer.wear.data

import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test

class ApiClientTest {
    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun requestsTheDedicatedFiveItemWatchRouteWithStoredToken() = runTest {
        val baseUrl = server.url("/").toString().trimEnd('/')
        val tokenStore = MemoryTokenStore("watch-token")
        val client = ApiClient(ConnectionReader { SavedConnection(baseUrl) }, tokenStore)
        val privateOrigin = "http://192.168.10.136:38522"
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{
                        "request_id":"request-1",
                        "batch_id":"batch-1",
                        "count":5,
                        "items":[
                            {"item_id":"1","media_id":"m1","thumbnail_url":"$privateOrigin/api/v1/media/m1/thumbnail","preview_url":"$privateOrigin/api/v1/media/m1/original"},
                            {"item_id":"2","media_id":"m2","thumbnail_url":"$privateOrigin/api/v1/media/m2/thumbnail","preview_url":"$privateOrigin/api/v1/media/m2/original"},
                            {"item_id":"3","media_id":"m3","thumbnail_url":"$privateOrigin/api/v1/media/m3/thumbnail","preview_url":"$privateOrigin/api/v1/media/m3/original"},
                            {"item_id":"4","media_id":"m4","thumbnail_url":"$privateOrigin/api/v1/media/m4/thumbnail","preview_url":"$privateOrigin/api/v1/media/m4/original"},
                            {"item_id":"5","media_id":"m5","thumbnail_url":"$privateOrigin/api/v1/media/m5/thumbnail","preview_url":"$privateOrigin/api/v1/media/m5/original"}
                        ]
                    }""".trimIndent(),
                ),
        )

        val response = client.watchRecommendations(RecommendationRequest("request-1"))
        val request = server.takeRequest()

        assertEquals(5, response.items.size)
        assertEquals("/api/v1/watch/recommendations", request.path)
        assertEquals("Bearer watch-token", request.getHeader("Authorization"))
        assertEquals("POST", request.method)
        assertEquals(
            server.url("/api/v1/media/m1/thumbnail").toString(),
            response.items.first().thumbnailUrl,
        )
        assertEquals(
            server.url("/api/v1/media/m1/original").toString(),
            response.items.first().previewUrl,
        )
    }
}

private class MemoryTokenStore(initial: String? = null) : TokenStore {
    private var value = initial
    override fun read(): String? = value
    override fun write(token: String) {
        value = token
    }
    override fun clear() {
        value = null
    }
}
