package com.zvec.lanviewer.ui

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import java.io.IOException

internal data class RecommendationPreloadOutcome(
    val failedCount: Int,
)

internal class RecommendationPreloadTask<T> internal constructor(
    val requestId: String,
    val generation: Long,
    private val criticalResponse: Deferred<T>,
    private val settled: Deferred<RecommendationPreloadOutcome>,
    private val job: Job,
) {
    suspend fun awaitCritical(): T = criticalResponse.await()

    suspend fun awaitSettled(): RecommendationPreloadOutcome = settled.await()

    fun cancel() = job.cancel()
}

internal fun <T> CoroutineScope.startRecommendationPreload(
    requestId: String,
    generation: Long,
    criticalCount: Int,
    create: suspend (String) -> T,
    thumbnailUrls: (T) -> List<String>,
    preload: suspend (String) -> Boolean,
): RecommendationPreloadTask<T> {
    require(criticalCount > 0)
    val criticalResponse = CompletableDeferred<T>()
    val settled = CompletableDeferred<RecommendationPreloadOutcome>()
    val job = launch {
        try {
            val response = create(requestId)
            coroutineScope {
                val loads = thumbnailUrls(response).map { url ->
                    async {
                        try {
                            preload(url)
                        } catch (error: CancellationException) {
                            throw error
                        } catch (_: Throwable) {
                            false
                        }
                    }
                }
                val criticalSize = minOf(criticalCount, loads.size)
                val criticalResults = loads.take(criticalSize).awaitAll()
                if (criticalResults.any { !it }) {
                    throw IOException("推荐关键缩略图预载失败")
                }
                criticalResponse.complete(response)
                val tailResults = loads.drop(criticalSize).awaitAll()
                settled.complete(
                    RecommendationPreloadOutcome(
                        failedCount = criticalResults.count { !it } + tailResults.count { !it },
                    ),
                )
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
