package com.zvec.lanviewer.data.network

import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class ResumableDownloaderTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        server = MockWebServer().apply { start() }
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun resumesExistingPartWithRangeRequest() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(206)
                .setHeader("Content-Range", "bytes 5-10/11")
                .setHeader("ETag", "\"sha256-value\"")
                .setBody(" world"),
        )
        val target = File(temporaryFolder.root, "original.jpg")
        File(temporaryFolder.root, "original.jpg.part").writeText("hello")

        val completed = ResumableDownloader(OkHttpClient()).download(
            server.url("/api/v1/media/m1/original").toString(),
            target,
        )

        assertEquals("hello world", completed.readText())
        assertEquals("bytes=5-", server.takeRequest().getHeader("Range"))
    }
}
