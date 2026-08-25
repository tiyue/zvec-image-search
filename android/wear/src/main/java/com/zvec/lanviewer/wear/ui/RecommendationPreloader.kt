package com.zvec.lanviewer.wear.ui

import android.content.Context
import coil.ImageLoader
import coil.request.CachePolicy
import coil.request.ErrorResult
import coil.request.ImageRequest
import coil.request.SuccessResult
import com.zvec.lanviewer.wear.data.RecommendationsResponse
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import java.io.IOException

fun interface ThumbnailLoader {
    suspend fun preload(url: String): Boolean
}

fun interface OriginalLoader {
    suspend fun preload(url: String): Boolean
}

class CoilThumbnailLoader(
    private val context: Context,
    private val imageLoader: ImageLoader,
) : ThumbnailLoader {
    override suspend fun preload(url: String): Boolean {
        val request = ImageRequest.Builder(context)
            .data(url)
            .size(240, 240)
            .memoryCachePolicy(CachePolicy.ENABLED)
            .diskCachePolicy(CachePolicy.ENABLED)
            .build()
        return when (val result = imageLoader.execute(request)) {
            is SuccessResult -> true
            is ErrorResult -> throw result.throwable
        }
    }
}

class CoilOriginalLoader(
    private val context: Context,
    private val imageLoader: ImageLoader,
) : OriginalLoader {
    override suspend fun preload(url: String): Boolean {
        return when (val result = imageLoader.execute(originalImageRequest(context, url))) {
            is SuccessResult -> true
            is ErrorResult -> throw result.throwable
        }
    }
}

internal fun originalImageRequest(context: Context, url: String): ImageRequest =
    ImageRequest.Builder(context)
        .data(url)
        .size(ORIGINAL_CACHE_SIZE, ORIGINAL_CACHE_SIZE)
        .memoryCachePolicy(CachePolicy.ENABLED)
        .diskCachePolicy(CachePolicy.ENABLED)
        .diskCacheKey(url)
        .build()

internal const val ORIGINAL_CACHE_SIZE = 2048

internal data class PreloadOutcome(
    val failedCount: Int,
    val lastError: Throwable? = null,
)

private data class PreloadAttempt(
    val loaded: Boolean,
    val error: Throwable? = null,
)

internal class RecommendationPreloadTask(
    val requestId: String,
    val generation: Long,
    private val criticalResponse: Deferred<RecommendationsResponse>,
    private val settled: Deferred<PreloadOutcome>,
    private val job: Job,
) {
    suspend fun awaitCritical(): RecommendationsResponse = criticalResponse.await()
    suspend fun awaitSettled(): PreloadOutcome = settled.await()
    fun cancel() = job.cancel()
}

internal fun CoroutineScope.startRecommendationPreload(
    requestId: String,
    generation: Long,
    criticalCount: Int,
    create: suspend (String) -> RecommendationsResponse,
    preload: suspend (String) -> Boolean,
): RecommendationPreloadTask {
    require(criticalCount > 0)
    val criticalResponse = CompletableDeferred<RecommendationsResponse>()
    val settled = CompletableDeferred<PreloadOutcome>()
    val job = launch {
        try {
            val response = create(requestId)
            val urls = response.items.map { it.thumbnailUrl }
            if (urls.isEmpty()) throw IOException("服务器没有返回推荐图片")
            coroutineScope {
                val results = Channel<PreloadAttempt>(urls.size)
                urls.forEach { url ->
                    launch {
                        val result = try {
                            if (preload(url)) {
                                PreloadAttempt(loaded = true)
                            } else {
                                PreloadAttempt(
                                    loaded = false,
                                    error = IOException("服务器返回的缩略图无法显示"),
                                )
                            }
                        } catch (error: CancellationException) {
                            throw error
                        } catch (error: Throwable) {
                            PreloadAttempt(loaded = false, error = error)
                        }
                        results.send(result)
                    }
                }
                val criticalTarget = minOf(criticalCount, urls.size)
                var succeeded = 0
                var failed = 0
                var lastError: Throwable? = null
                repeat(urls.size) {
                    val result = results.receive()
                    if (result.loaded) {
                        succeeded += 1
                    } else {
                        failed += 1
                        lastError = result.error
                    }
                    if (succeeded >= criticalTarget && !criticalResponse.isCompleted) {
                        criticalResponse.complete(response)
                    }
                }
                if (!criticalResponse.isCompleted) {
                    throw lastError ?: IOException("推荐缩略图加载失败")
                }
                settled.complete(PreloadOutcome(failedCount = failed, lastError = lastError))
            }
        } catch (error: CancellationException) {
            criticalResponse.cancel(error)
            settled.cancel(error)
        } catch (error: Throwable) {
            criticalResponse.completeExceptionally(error)
            settled.completeExceptionally(error)
        }
    }
    return RecommendationPreloadTask(
        requestId = requestId,
        generation = generation,
        criticalResponse = criticalResponse,
        settled = settled,
        job = job,
    )
}
