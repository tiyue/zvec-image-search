package com.zvec.lanviewer.data.repository

import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.OutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

class OriginalFileCopyTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    private var sourceIndex = 0

    @Test
    fun streamsCompleteContentAcrossMultipleChunks() = runBlocking {
        val bytes = ByteArray(LARGE_BYTES) { index -> (index % 251).toByte() }
        val source = fileWith(bytes)
        val sink = ByteArrayOutputStream()

        copyOriginalFile(source, { sink })

        assertArrayEquals(bytes, sink.toByteArray())
    }

    @Test
    fun opensAndCopiesOnTheProvidedDispatcherWithoutBlockingTheCaller() = runBlocking {
        val workerName = "original-file-copy-test"
        val executor = Executors.newSingleThreadExecutor { runnable -> Thread(runnable, workerName) }
        val dispatcher = executor.asCoroutineDispatcher()
        try {
            val source = fileWith(ByteArray(LARGE_BYTES) { 3 })
            val openEntered = CountDownLatch(1)
            val releaseOpen = CountDownLatch(1)
            val openThread = AtomicReference<Thread>()
            val writeThread = AtomicReference<Thread>()
            val sink = object : OutputStream() {
                override fun write(b: Int) {
                    writeThread.set(Thread.currentThread())
                }

                override fun write(b: ByteArray, off: Int, len: Int) {
                    writeThread.set(Thread.currentThread())
                }
            }
            val copy = async(start = CoroutineStart.UNDISPATCHED) {
                copyOriginalFile(
                    source,
                    {
                        openThread.set(Thread.currentThread())
                        openEntered.countDown()
                        releaseOpen.await(TIMEOUT_SECONDS, TimeUnit.SECONDS)
                        sink
                    },
                    dispatcher,
                )
            }

            assertTrue(openEntered.await(TIMEOUT_SECONDS, TimeUnit.SECONDS))

            // The caller coroutine keeps making progress while the copy is parked inside the
            // destination open on the IO dispatcher.
            val heartbeats = AtomicInteger()
            launch { heartbeats.incrementAndGet() }.join()
            assertEquals(1, heartbeats.get())

            releaseOpen.countDown()
            copy.await()

            assertTrue(openThread.get().name.startsWith(workerName))
            assertEquals(openThread.get(), writeThread.get())
            assertNotEquals(Thread.currentThread(), openThread.get())
        } finally {
            executor.shutdownNow()
        }
    }

    @Test
    fun cancellationBeforeOpenNeverOpensTheDestination() = runBlocking {
        val source = fileWith(ByteArray(4096) { 1 })
        val opened = AtomicBoolean(false)
        val job = launch(Dispatchers.IO, start = CoroutineStart.LAZY) {
            copyOriginalFile(source, { opened.set(true); ByteArrayOutputStream() })
        }

        job.cancel()
        job.start()
        job.join()

        assertTrue(job.isCancelled)
        assertFalse(opened.get())
    }

    @Test
    fun cancellationWhileQueuedOnTheDispatcherSkipsTheDestinationOpen() = runBlocking {
        val worker = Executors.newSingleThreadExecutor { runnable -> Thread(runnable, "queued-worker") }
        val dispatcher = worker.asCoroutineDispatcher()
        try {
            val source = fileWith(ByteArray(4096) { 2 })
            val opened = AtomicBoolean(false)
            val blockerRunning = CountDownLatch(1)
            val releaseBlocker = CountDownLatch(1)
            val blocker = launch(dispatcher) {
                blockerRunning.countDown()
                releaseBlocker.await(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            }
            assertTrue(blockerRunning.await(TIMEOUT_SECONDS, TimeUnit.SECONDS))

            val copy = launch(dispatcher) {
                copyOriginalFile(source, { opened.set(true); ByteArrayOutputStream() })
            }
            copy.cancel()
            releaseBlocker.countDown()
            blocker.join()
            copy.join()

            assertTrue(copy.isCancelled)
            assertFalse(opened.get())
        } finally {
            worker.shutdownNow()
        }
    }

    @Test
    fun cancellingDuringCopyDoesNotCompleteAndClosesDestination() = runBlocking {
        val source = fileWith(ByteArray(LARGE_BYTES) { 5 })
        val writeEntered = CountDownLatch(1)
        val releaseWrite = CountDownLatch(1)
        val closed = AtomicBoolean(false)
        val sink = object : OutputStream() {
            override fun write(b: Int) {
                writeEntered.countDown()
                releaseWrite.await(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            }

            override fun write(b: ByteArray, off: Int, len: Int) {
                writeEntered.countDown()
                releaseWrite.await(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            }

            override fun close() {
                closed.set(true)
            }
        }
        val job = launch(Dispatchers.IO) {
            copyOriginalFile(source, { sink })
        }

        assertTrue(writeEntered.await(TIMEOUT_SECONDS, TimeUnit.SECONDS))
        job.cancel()
        releaseWrite.countDown()
        job.join()

        assertTrue(job.isCancelled)
        assertTrue(closed.get())
    }

    @Test
    fun destinationOpenFailurePropagatesIOException() = runBlocking {
        val source = fileWith(ByteArray(16))
        try {
            copyOriginalFile(source, { throw IOException("目标不可写") })
            fail("expected IOException")
        } catch (error: IOException) {
            assertEquals("目标不可写", error.message)
        }
    }

    @Test
    fun nullDestinationStreamReportsClearIOException() = runBlocking {
        val source = fileWith(ByteArray(16))
        try {
            copyOriginalFile(source, { null })
            fail("expected IOException")
        } catch (error: IOException) {
            assertEquals("无法打开保存位置", error.message)
        }
    }

    @Test
    fun writeFailurePropagatesIOExceptionAndClosesDestination() = runBlocking {
        val source = fileWith(ByteArray(16))
        val closed = AtomicBoolean(false)
        val sink = object : OutputStream() {
            override fun write(b: Int) {
                throw IOException("写入失败")
            }

            override fun write(b: ByteArray, off: Int, len: Int) {
                throw IOException("写入失败")
            }

            override fun close() {
                closed.set(true)
            }
        }
        try {
            copyOriginalFile(source, { sink })
            fail("expected IOException")
        } catch (error: IOException) {
            assertEquals("写入失败", error.message)
        }
        assertTrue(closed.get())
    }

    private fun fileWith(bytes: ByteArray): File =
        File(temporaryFolder.root, "source-${sourceIndex++}.bin").apply { writeBytes(bytes) }

    companion object {
        private const val TIMEOUT_SECONDS = 10L
        private const val LARGE_BYTES = 768 * 1024 + 4096
    }
}
