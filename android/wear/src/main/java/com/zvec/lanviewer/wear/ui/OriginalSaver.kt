package com.zvec.lanviewer.wear.ui

import android.content.ContentValues
import android.content.Context
import android.os.Environment
import android.provider.MediaStore
import android.webkit.MimeTypeMap
import coil.ImageLoader
import coil.annotation.ExperimentalCoilApi
import coil.request.CachePolicy
import coil.request.ErrorResult
import coil.request.SuccessResult
import com.zvec.lanviewer.wear.data.RecommendationItem
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.util.Locale
import kotlin.coroutines.coroutineContext

fun interface OriginalSaver {
    suspend fun save(item: RecommendationItem)
}

@OptIn(ExperimentalCoilApi::class)
class MediaStoreOriginalSaver(
    context: Context,
    private val imageLoader: ImageLoader,
) : OriginalSaver {
    private val appContext = context.applicationContext

    override suspend fun save(item: RecommendationItem) = withContext(Dispatchers.IO) {
        val request = originalImageRequest(appContext, item.previewUrl)
            .newBuilder()
            .memoryCachePolicy(CachePolicy.DISABLED)
            .build()
        val result = imageLoader.execute(request)
        if (result !is SuccessResult) {
            val cause = (result as? ErrorResult)?.throwable
            throw WatchOperationException(
                WatchErrorType.ORIGINAL,
                cause?.message?.takeIf(String::isNotBlank) ?: "无法下载当前原图",
                cause,
            )
        }
        val cacheKey = result.diskCacheKey ?: item.previewUrl
        val diskCache = imageLoader.diskCache ?: throw WatchOperationException(
            WatchErrorType.ORIGINAL,
            "原图磁盘缓存不可用",
        )
        val snapshot = diskCache.openSnapshot(cacheKey)
            ?: throw WatchOperationException(
                WatchErrorType.ORIGINAL,
                "缓存中找不到完整原图",
            )
        snapshot.use { cached ->
            val displayName = savedOriginalDisplayName(item.name, System.currentTimeMillis())
            val resolver = appContext.contentResolver
            val values = ContentValues().apply {
                put(MediaStore.Images.Media.DISPLAY_NAME, displayName)
                put(MediaStore.Images.Media.MIME_TYPE, originalMimeType(displayName))
                put(
                    MediaStore.Images.Media.RELATIVE_PATH,
                    "${Environment.DIRECTORY_PICTURES}/YaoLens",
                )
                put(MediaStore.Images.Media.IS_PENDING, 1)
            }
            val destination = resolver.insert(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                values,
            ) ?: throw WatchOperationException(
                WatchErrorType.STORAGE,
                "无法在手表相册中创建文件",
            )
            var published = false
            try {
                val output = resolver.openOutputStream(destination, "w")
                    ?: throw WatchOperationException(
                        WatchErrorType.STORAGE,
                        "无法打开手表相册的写入位置",
                    )
                output.use { sink ->
                    cached.data.toFile().inputStream().use { source ->
                        val buffer = ByteArray(SAVE_BUFFER_BYTES)
                        while (true) {
                            coroutineContext.ensureActive()
                            val read = source.read(buffer)
                            if (read < 0) break
                            sink.write(buffer, 0, read)
                        }
                    }
                }
                val publishedValues = ContentValues().apply {
                    put(MediaStore.Images.Media.IS_PENDING, 0)
                }
                if (resolver.update(destination, publishedValues, null, null) != 1) {
                    throw WatchOperationException(
                        WatchErrorType.STORAGE,
                        "图片已写入，但系统无法发布到相册",
                    )
                }
                published = true
            } catch (error: CancellationException) {
                throw error
            } catch (error: WatchOperationException) {
                throw error
            } catch (error: Throwable) {
                val reason = if (error.hasNoSpaceReason()) {
                    "手表存储空间不足"
                } else {
                    error.message?.takeIf(String::isNotBlank) ?: "写入手表相册失败"
                }
                throw WatchOperationException(WatchErrorType.STORAGE, reason, error)
            } finally {
                if (!published) resolver.delete(destination, null, null)
            }
        }
    }
}

internal fun savedOriginalDisplayName(rawName: String, nowMillis: Long): String {
    val leaf = rawName
        .substringAfterLast('/')
        .substringAfterLast('\\')
        .trim()
        .replace(INVALID_FILE_NAME_CHARS, "_")
    return leaf.ifBlank { "YaoLens-$nowMillis.jpg" }
}

private fun originalMimeType(displayName: String): String {
    val extension = displayName.substringAfterLast('.', "").lowercase(Locale.ROOT)
    return MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension)
        ?: when (extension) {
            "arw" -> "image/x-sony-arw"
            "heic" -> "image/heic"
            "heif" -> "image/heif"
            else -> "application/octet-stream"
        }
}

private fun Throwable.hasNoSpaceReason(): Boolean {
    var current: Throwable? = this
    repeat(6) {
        val message = current?.message.orEmpty()
        if (message.contains("ENOSPC", ignoreCase = true) ||
            message.contains("no space", ignoreCase = true)
        ) {
            return true
        }
        current = current?.cause
    }
    return false
}

private const val SAVE_BUFFER_BYTES = 256 * 1024
private val INVALID_FILE_NAME_CHARS = Regex("[\\\\/:*?\"<>|]")
