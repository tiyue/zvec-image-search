package com.zvec.lanviewer.wear.ui

import android.content.Context
import coil.ImageLoader
import coil.request.CachePolicy
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
        return imageLoader.execute(request) is SuccessResult
    }
}

class CoilOriginalLoader(
    private val context: Context,
    private val imageLoader: ImageLoader,
) : OriginalLoader {
    override suspend fun preload(url: String): Boolean {
        val request = ImageRequest.Builder(context)
            .data(url)
            .size(ORIGINAL_CACHE_SIZE, ORIGINAL_CACHE_SIZE)
            .memoryCachePolicy(CachePolicy.ENABLED)
            .diskCachePolicy(CachePolicy.ENABLED)
            .build()
        return imageLoader.execute(request) is SuccessResult
    }
}

internal const val ORIGINAL_CACHE_SIZE = 2048

internal data class PreloadOutcome(val failedCount: Int)

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
                val results = Channel<Boolean>(urls.size)
                urls.forEach { url ->
                    launch {
                        val loaded = try {
                            preload(url)
                        } catch (error: CancellationException) {
                            throw error
                        } catch (_: Throwable) {
                            false
                        }
                        results.send(loaded)
                    }
                }
                val criticalTarget = minOf(criticalCount, urls.size)
                var succeeded = 0
                var failed = 0
                repeat(urls.size) {
                    if (results.receive()) succeeded += 1 else failed += 1
                    if (succeeded >= criticalTarget && !criticalResponse.isCompleted) {
                        criticalResponse.complete(response)
                    }
                }
                if (!criticalResponse.isCompleted) {
                    throw IOException("推荐缩略图加载失败")
                }
                settled.complete(PreloadOutcome(failedCount = failed))
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
