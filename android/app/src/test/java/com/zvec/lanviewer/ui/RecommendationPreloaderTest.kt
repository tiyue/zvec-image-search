package com.zvec.lanviewer.ui

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

@OptIn(ExperimentalCoroutinesApi::class)
class RecommendationPreloaderTest {
    @Test
    fun startsAllFifteenButReleasesTheBatchAfterTheFirstFour() = runTest {
        val gates = List(15) { CompletableDeferred<Boolean>() }
        val starts = IntArray(15)
        var creates = 0
        val task = startRecommendationPreload(
            requestId = "request-1",
            generation = 7,
            criticalCount = 4,
            create = {
                creates += 1
                (0 until 15).toList()
            },
            thumbnailUrls = { items -> items.map(Int::toString) },
            preload = { index ->
                starts[index.toInt()] += 1
                gates[index.toInt()].await()
            },
        )
        val critical = async { task.awaitCritical() }
        val settled = async { task.awaitSettled() }

        runCurrent()
        assertEquals(15, starts.count { it == 1 })
        gates.take(4).forEach { it.complete(true) }
        runCurrent()

        assertTrue(critical.isCompleted)
        assertEquals((0 until 15).toList(), critical.await())
        assertFalse(settled.isCompleted)
        gates.drop(4).forEach { it.complete(true) }

        assertEquals(0, settled.await().failedCount)
        assertEquals(1, creates)
        assertTrue(starts.all { it == 1 })
    }

    @Test
    fun twoConsumersReuseTheSameCreateAndThumbnailRequests() = runTest {
        val gates = List(6) { CompletableDeferred<Boolean>() }
        var creates = 0
        val starts = IntArray(6)
        val task = startRecommendationPreload(
            requestId = "request-2",
            generation = 1,
            criticalCount = 4,
            create = {
                creates += 1
                (0 until 6).toList()
            },
            thumbnailUrls = { items -> items.map(Int::toString) },
            preload = { index ->
                starts[index.toInt()] += 1
                gates[index.toInt()].await()
            },
        )
        val first = async { task.awaitCritical() }
        val takeover = async { task.awaitCritical() }

        runCurrent()
        gates.take(4).forEach { it.complete(true) }
        runCurrent()

        assertEquals(first.await(), takeover.await())
        assertEquals(1, creates)
        assertTrue(starts.all { it == 1 })
        gates.drop(4).forEach { it.complete(true) }
        assertEquals(0, task.awaitSettled().failedCount)
    }

    @Test
    fun criticalFailureRejectsTheBatchAndCancelsTheTail() = runTest {
        val gates = List(15) { CompletableDeferred<Boolean>() }
        val task = startRecommendationPreload(
            requestId = "request-3",
            generation = 1,
            criticalCount = 4,
            create = { (0 until 15).toList() },
            thumbnailUrls = { items -> items.map(Int::toString) },
            preload = { index -> gates[index.toInt()].await() },
        )

        runCurrent()
        gates.take(3).forEach { it.complete(true) }
        gates[3].complete(false)

        val error = runCatching { task.awaitCritical() }.exceptionOrNull()
        assertTrue(error is IOException)
        assertTrue(runCatching { task.awaitSettled() }.exceptionOrNull() is IOException)
    }

    @Test
    fun tailFailureDoesNotRollBackTheCriticalBatch() = runTest {
        val gates = List(15) { CompletableDeferred<Boolean>() }
        val task = startRecommendationPreload(
            requestId = "request-4",
            generation = 1,
            criticalCount = 4,
            create = { (0 until 15).toList() },
            thumbnailUrls = { items -> items.map(Int::toString) },
            preload = { index -> gates[index.toInt()].await() },
        )

        runCurrent()
        gates.take(4).forEach { it.complete(true) }
        assertEquals((0 until 15).toList(), task.awaitCritical())
        gates.drop(4).forEachIndexed { index, gate -> gate.complete(index != 3) }

        assertEquals(1, task.awaitSettled().failedCount)
    }

    @Test
    fun fewerThanFourItemsWaitsForEveryThumbnail() = runTest {
        val gates = List(3) { CompletableDeferred<Boolean>() }
        val task = startRecommendationPreload(
            requestId = "request-5",
            generation = 1,
            criticalCount = 4,
            create = { (0 until 3).toList() },
            thumbnailUrls = { items -> items.map(Int::toString) },
            preload = { index -> gates[index.toInt()].await() },
        )
        val critical = async { task.awaitCritical() }

        runCurrent()
        gates.take(2).forEach { it.complete(true) }
        runCurrent()
        assertFalse(critical.isCompleted)
        gates.last().complete(true)

        assertEquals((0 until 3).toList(), critical.await())
        assertEquals(0, task.awaitSettled().failedCount)
    }

    @Test
    fun cancellationPreventsLateCriticalCompletion() = runTest {
        val gates = List(15) { CompletableDeferred<Boolean>() }
        val task = startRecommendationPreload(
            requestId = "request-6",
            generation = 9,
            criticalCount = 4,
            create = { (0 until 15).toList() },
            thumbnailUrls = { items -> items.map(Int::toString) },
            preload = { index -> gates[index.toInt()].await() },
        )

        runCurrent()
        task.cancel()
        gates.forEach { it.complete(true) }

        assertEquals(9, task.generation)
        assertTrue(runCatching { task.awaitCritical() }.exceptionOrNull() is CancellationException)
        assertTrue(runCatching { task.awaitSettled() }.exceptionOrNull() is CancellationException)
    }
}
