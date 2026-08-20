package com.zvec.lanviewer.data.network

import com.zvec.lanviewer.data.model.DownloadProgress
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import kotlin.coroutines.coroutineContext

/** Downloads one original file with HTTP Range resume into an atomic .part file. */
class ResumableDownloader(private val httpClient: OkHttpClient) {
    suspend fun download(
        url: String,
        target: File,
        onProgress: (DownloadProgress) -> Unit = {},
    ): File = withContext(Dispatchers.IO) {
        if (target.isFile) return@withContext target
        target.parentFile?.mkdirs()
        val part = File(target.parentFile, "${target.name}.part")
        val etagFile = File(target.parentFile, "${target.name}.etag")

        var restarted = false
        while (true) {
            coroutineContext.ensureActive()
            val existing = part.takeIf(File::isFile)?.length() ?: 0L
            val builder = Request.Builder().url(url).get()
            if (existing > 0L) {
                builder.header("Range", "bytes=$existing-")
                etagFile.takeIf(File::isFile)?.readText()?.takeIf(String::isNotBlank)?.let {
                    builder.header("If-Range", it)
                }
            }

            val call = httpClient.newCall(builder.build())
            val response = call.awaitResponse()
            response.use { current ->
                if (current.code == 416 && !restarted) {
                    part.delete()
                    etagFile.delete()
                    restarted = true
                    return@use
                }
                if (current.code !in setOf(200, 206)) {
                    throw IOException("原图下载失败（HTTP ${current.code}）")
                }

                val append = current.code == 206 && existing > 0L
                if (append && !contentRangeStartsAt(current.header("Content-Range"), existing)) {
                    throw IOException("服务器返回了不匹配的续传范围")
                }
                if (!append) part.delete()
                current.header("ETag")?.let(etagFile::writeText)
                val initial = if (append) existing else 0L
                val total = parseTotalBytes(current.header("Content-Range"))
                    ?: current.body?.contentLength()?.takeIf { it >= 0L }?.plus(initial)

                val body = current.body ?: throw IOException("原图响应为空")
                try {
                    body.byteStream().use { input ->
                        FileOutputStream(part, append).use { output ->
                            val buffer = ByteArray(BUFFER_BYTES)
                            var written = initial
                            var lastReportedBytes = initial
                            var lastReportedAtNanos = System.nanoTime()
                            while (true) {
                                coroutineContext.ensureActive()
                                val read = input.read(buffer)
                                if (read < 0) break
                                output.write(buffer, 0, read)
                                written += read
                                val now = System.nanoTime()
                                if (written - lastReportedBytes >= PROGRESS_BYTES ||
                                    now - lastReportedAtNanos >= PROGRESS_INTERVAL_NANOS
                                ) {
                                    onProgress(DownloadProgress(written, total))
                                    lastReportedBytes = written
                                    lastReportedAtNanos = now
                                }
                            }
                            if (written != lastReportedBytes || written == initial) {
                                onProgress(DownloadProgress(written, total))
                            }
                            output.fd.sync()
                        }
                    }
                } catch (error: Throwable) {
                    call.cancel()
                    throw error
                }
                if (target.exists() && !target.delete()) {
                    throw IOException("无法替换旧的本地缓存")
                }
                if (!part.renameTo(target)) {
                    throw IOException("无法完成原图缓存写入")
                }
                etagFile.delete()
                return@withContext target
            }
            // A single 416 response reaches here after clearing stale partial state.
        }
        @Suppress("UNREACHABLE_CODE")
        throw IOException("无法完成原图下载")
    }

    private fun contentRangeStartsAt(header: String?, expected: Long): Boolean {
        val match = CONTENT_RANGE.find(header.orEmpty()) ?: return false
        return match.groupValues[1].toLongOrNull() == expected
    }

    private fun parseTotalBytes(header: String?): Long? =
        CONTENT_RANGE.find(header.orEmpty())?.groupValues?.getOrNull(3)?.toLongOrNull()

    companion object {
        private const val BUFFER_BYTES = 256 * 1024
        private const val PROGRESS_BYTES = 1024 * 1024L
        private const val PROGRESS_INTERVAL_NANOS = 100_000_000L
        private val CONTENT_RANGE = Regex("bytes\\s+(\\d+)-(\\d+)/(\\d+|\\*)", RegexOption.IGNORE_CASE)
    }
}
