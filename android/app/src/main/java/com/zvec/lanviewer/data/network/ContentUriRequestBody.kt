package com.zvec.lanviewer.data.network

import android.content.ContentResolver
import android.net.Uri
import android.os.CancellationSignal
import com.zvec.lanviewer.data.model.UploadProgress
import okhttp3.MediaType
import okhttp3.RequestBody
import okio.BufferedSink
import java.io.InterruptedIOException
import java.util.concurrent.atomic.AtomicBoolean

/** Streams a ContentResolver URI without imposing a fixed byte limit or buffering the file. */
class ContentUriRequestBody(
    private val contentResolver: ContentResolver,
    private val uri: Uri,
    private val mediaType: MediaType?,
    private val onProgress: (UploadProgress) -> Unit,
) : RequestBody() {
    private val cancelled = AtomicBoolean(false)
    private val cancellationSignal = CancellationSignal()
    private val knownLength: Long by lazy {
        runCatching {
            contentResolver.openAssetFileDescriptor(uri, "r", cancellationSignal)?.use { descriptor ->
                descriptor.length.takeIf { it >= 0L } ?: -1L
            } ?: -1L
        }.getOrDefault(-1L)
    }

    override fun contentType(): MediaType? = mediaType

    override fun contentLength(): Long = knownLength

    override fun writeTo(sink: BufferedSink) {
        var written = 0L
        var lastReportedBytes = 0L
        var lastReportedAtNanos = System.nanoTime()
        val total = knownLength.takeIf { it >= 0L }
        val input = contentResolver.openInputStream(uri)
            ?: throw java.io.FileNotFoundException("无法读取所选查询图片")
        input.use { stream ->
            val buffer = ByteArray(BUFFER_BYTES)
            while (true) {
                if (cancelled.get() || Thread.currentThread().isInterrupted) {
                    throw InterruptedIOException("上传已取消")
                }
                val read = stream.read(buffer)
                if (read < 0) break
                sink.write(buffer, 0, read)
                written += read
                val now = System.nanoTime()
                if (written - lastReportedBytes >= PROGRESS_BYTES ||
                    now - lastReportedAtNanos >= PROGRESS_INTERVAL_NANOS
                ) {
                    onProgress(UploadProgress(written, total))
                    lastReportedBytes = written
                    lastReportedAtNanos = now
                }
            }
        }
        if (written != lastReportedBytes || written == 0L) {
            onProgress(UploadProgress(written, total))
        }
    }

    fun cancel() {
        cancelled.set(true)
        cancellationSignal.cancel()
    }

    companion object {
        private const val BUFFER_BYTES = 256 * 1024
        private const val PROGRESS_BYTES = 1024 * 1024L
        private const val PROGRESS_INTERVAL_NANOS = 100_000_000L
    }
}
