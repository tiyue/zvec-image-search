package com.zvec.lanviewer.data.repository

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException
import java.io.OutputStream
import kotlin.coroutines.coroutineContext

/**
 * Streams a cached original file into a destination opened on [dispatcher], so the blocking
 * open/read/write work never runs on the caller's thread.
 */
internal suspend fun copyOriginalFile(
    source: File,
    openDestination: () -> OutputStream?,
    dispatcher: CoroutineDispatcher = Dispatchers.IO,
) = withContext(dispatcher) {
    coroutineContext.ensureActive()
    val output = openDestination() ?: throw IOException("无法打开保存位置")
    output.use { sink ->
        source.inputStream().use { input ->
            val buffer = ByteArray(BUFFER_BYTES)
            while (true) {
                coroutineContext.ensureActive()
                val read = input.read(buffer)
                if (read < 0) break
                sink.write(buffer, 0, read)
            }
            coroutineContext.ensureActive()
        }
    }
}

private const val BUFFER_BYTES = 256 * 1024
