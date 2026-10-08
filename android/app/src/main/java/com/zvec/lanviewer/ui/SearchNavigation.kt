package com.zvec.lanviewer.ui

import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue

internal enum class MobileTab { SEARCH, RESULTS, RECOMMENDATIONS, DEVICE }

internal fun mobileTabAfterPhaseChange(
    selectedTab: MobileTab,
    phase: ConnectionPhase,
): MobileTab = if (phase == ConnectionPhase.READY) selectedTab else MobileTab.SEARCH

/** The tab to show once a first search page completes, and whether the wait is still armed. */
internal data class SearchNavigationDecision(
    val targetTab: MobileTab,
    val awaitingFirstResult: Boolean,
)

/**
 * Single-shot automatic navigation to the results page.
 *
 * The phone jumps to results only when the user submitted a search and is still waiting on the
 * search page. The wait is always consumed by the completion, so a later page append and returning
 * to the search page never move the user again, and a manual choice of another tab is preserved.
 */
internal fun searchNavigationAfterCompletion(
    awaitingFirstResult: Boolean,
    currentTab: MobileTab,
    completionSignal: Long = 1L,
    handledCompletionSignal: Long = 0L,
): SearchNavigationDecision = when {
    completionSignal == 0L -> SearchNavigationDecision(currentTab, false)
    completionSignal == handledCompletionSignal -> SearchNavigationDecision(currentTab, awaitingFirstResult)
    else -> SearchNavigationDecision(
        targetTab = if (awaitingFirstResult && currentTab == MobileTab.SEARCH) MobileTab.RESULTS else currentTab,
        awaitingFirstResult = false,
    )
}

/**
 * Updates the wait after the user taps a tab. Only staying on the search page keeps the wait
 * armed; switching to results, devices or recommendations revokes the pending auto navigation.
 */
internal fun searchNavigationAfterTabSelected(
    awaitingFirstResult: Boolean,
    selectedTab: MobileTab,
): Boolean = awaitingFirstResult && selectedTab == MobileTab.SEARCH

/**
 * Keeps the search grid anchored to the current local search identity: a new search resets the
 * position, while a configuration change keeps the scroll of the query being browsed because the
 * guard is itself saveable.
 */
@Composable
internal fun SearchGridAnchorEffect(gridState: LazyGridState, searchAttempt: Long) {
    var anchoredAttempt by rememberSaveable { mutableStateOf(searchAttempt) }
    LaunchedEffect(searchAttempt) {
        if (searchAttempt != anchoredAttempt) {
            anchoredAttempt = searchAttempt
            gridState.scrollToItem(0)
        }
    }
}
