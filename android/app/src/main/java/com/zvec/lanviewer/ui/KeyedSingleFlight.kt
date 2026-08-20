package com.zvec.lanviewer.ui

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

/**
 * Owns one cancellable operation at a time.
 *
 * Repeated taps for the same key join the active attempt (no second HTTP pairing request),
 * while choosing another computer cancels the stale attempt before starting the replacement.
 * This class is intentionally small and Android-free so its race semantics can be unit tested.
 */
internal class KeyedSingleFlight(private val scope: CoroutineScope) {
    private var activeKey: String? = null
    private var activeJob: Job? = null

    fun launch(
        key: String,
        onComplete: ((Throwable?) -> Unit)? = null,
        block: suspend () -> Unit,
    ): Boolean {
        if (activeKey == key && activeJob?.isActive == true) return false

        activeJob?.cancel()
        val next = scope.launch(start = CoroutineStart.LAZY) { block() }
        activeKey = key
        activeJob = next
        next.invokeOnCompletion { error ->
            // A cancelled older job may complete after its replacement starts.
            if (activeJob === next) {
                activeJob = null
                activeKey = null
                onComplete?.invoke(error)
            }
        }
        next.start()
        return true
    }

    fun cancel() {
        activeJob?.cancel()
        activeJob = null
        activeKey = null
    }
}

/** Rejects late results from an operation that has been replaced or cancelled. */
internal class OperationGeneration {
    private var current: Any = Any()

    fun begin(): Any = Any().also { current = it }

    fun invalidate() {
        current = Any()
    }

    fun isCurrent(candidate: Any): Boolean = candidate === current
}
