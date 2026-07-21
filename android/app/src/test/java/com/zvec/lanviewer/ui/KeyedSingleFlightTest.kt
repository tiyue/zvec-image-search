package com.zvec.lanviewer.ui

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class KeyedSingleFlightTest {
    @Test
    fun repeatedConnectionTapJoinsExistingAttemptAndCanRetryAfterCompletion() = runTest {
        val flight = KeyedSingleFlight(this)
        val release = CompletableDeferred<Unit>()
        var runs = 0

        assertTrue(flight.launch("http://192.168.1.20:38522") {
            runs += 1
            release.await()
        })
        runCurrent()
        assertFalse(flight.launch("http://192.168.1.20:38522") { runs += 1 })
        assertTrue(runs == 1)

        release.complete(Unit)
        advanceUntilIdle()
        assertTrue(flight.launch("http://192.168.1.20:38522") { runs += 1 })
        advanceUntilIdle()
        assertTrue(runs == 2)
    }

    @Test
    fun choosingAnotherComputerCancelsStaleConnectionBeforeStartingNewOne() = runTest {
        val flight = KeyedSingleFlight(this)
        val firstStarted = CompletableDeferred<Unit>()
        val firstCancelled = CompletableDeferred<Unit>()
        val secondStarted = CompletableDeferred<Unit>()
        var staleCompletionCalled = false

        assertTrue(flight.launch("http://192.168.1.20:38522", onComplete = { staleCompletionCalled = true }) {
            firstStarted.complete(Unit)
            try {
                awaitCancellation()
            } finally {
                firstCancelled.complete(Unit)
            }
        })
        runCurrent()
        assertTrue(firstStarted.isCompleted)

        assertTrue(flight.launch("http://192.168.1.21:38522") {
            secondStarted.complete(Unit)
        })
        advanceUntilIdle()

        assertTrue(firstCancelled.isCompleted)
        assertTrue(secondStarted.isCompleted)
        assertFalse(staleCompletionCalled)
    }

    @Test
    fun completionCallbackRunsAfterSlotIsClearSoRecoveryCanStart() = runTest {
        val flight = KeyedSingleFlight(this)
        var recoveryStarted = false

        assertTrue(
            flight.launch("restore", onComplete = {
                assertTrue(flight.launch("discovery") { recoveryStarted = true })
            }) {},
        )
        advanceUntilIdle()

        assertTrue(recoveryStarted)
    }

    @Test
    fun manualConnectionInvalidatesLateRediscoveryResult() {
        val generation = OperationGeneration()
        val rediscovery = generation.begin()
        assertTrue(generation.isCurrent(rediscovery))

        // Starting a manual connection cancels discovery and invalidates its result token.
        generation.invalidate()

        assertFalse(generation.isCurrent(rediscovery))
        val laterDiscovery = generation.begin()
        assertTrue(generation.isCurrent(laterDiscovery))
    }
}
