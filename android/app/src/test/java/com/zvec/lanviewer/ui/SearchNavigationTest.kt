package com.zvec.lanviewer.ui

import com.zvec.lanviewer.data.model.SearchItem
import kotlinx.collections.immutable.persistentListOf
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SearchNavigationTest {
    @Test
    fun restoringDuringTheNextSearchDoesNotHandleThePreviousCompletionAgain() {
        val decision = searchNavigationAfterCompletion(
            awaitingFirstResult = true,
            currentTab = MobileTab.SEARCH,
            completionSignal = 1L,
            handledCompletionSignal = 1L,
        )
        assertEquals(MobileTab.SEARCH, decision.targetTab)
        assertTrue(decision.awaitingFirstResult)
        assertEquals(
            MobileTab.RESULTS,
            searchNavigationAfterCompletion(true, MobileTab.SEARCH, 2L, 1L).targetTab,
        )
    }

    @Test
    fun pendingSaveKeepsTheSelectedMediaWhenTheViewerMoves() {
        val first = SearchItem(mediaId = "first", name = "first.jpg", sizeBytes = 1234L)
        val second = SearchItem(mediaId = "second", name = "second.jpg")
        val target = PendingSaveTarget.of(first, ViewerSource.SEARCH)
        val state = AppUiState(
            phase = ConnectionPhase.READY,
            results = persistentListOf(first, second),
            viewerSource = ViewerSource.SEARCH,
            viewerIndex = 1,
        )
        assertSame(first, target.resolve(state))
        assertSame(first, target.resolve(state.copy(viewerSource = null, viewerIndex = null)))
    }

    @Test
    fun pendingSaveIsInvalidAfterItsSearchHasBeenReplacedOrLost() {
        val first = SearchItem(mediaId = "first", name = "first.jpg")
        val target = PendingSaveTarget.of(first, ViewerSource.SEARCH)
        assertNull(target.resolve(AppUiState(phase = ConnectionPhase.READY)))
        assertNull(target.resolve(AppUiState(results = persistentListOf(first))))
    }

    @Test
    fun firstPageCompletionFromWaitingSearchPageJumpsToResultsOnce() {
        val decision = searchNavigationAfterCompletion(
            awaitingFirstResult = true,
            currentTab = MobileTab.SEARCH,
        )

        assertEquals(MobileTab.RESULTS, decision.targetTab)
        assertFalse(decision.awaitingFirstResult)
    }

    @Test
    fun completionKeepsAnActivelyChosenPageAndConsumesTheWait() {
        listOf(MobileTab.DEVICE, MobileTab.RECOMMENDATIONS, MobileTab.RESULTS).forEach { tab ->
            val decision = searchNavigationAfterCompletion(awaitingFirstResult = false, currentTab = tab)

            assertEquals(tab, decision.targetTab)
            assertFalse(decision.awaitingFirstResult)
        }
    }

    @Test
    fun manualTabSelectionRevokesThePendingWaitExceptOnTheSearchPage() {
        assertFalse(searchNavigationAfterTabSelected(awaitingFirstResult = true, selectedTab = MobileTab.DEVICE))
        assertFalse(searchNavigationAfterTabSelected(awaitingFirstResult = true, selectedTab = MobileTab.RECOMMENDATIONS))
        assertFalse(searchNavigationAfterTabSelected(awaitingFirstResult = true, selectedTab = MobileTab.RESULTS))
        assertTrue(searchNavigationAfterTabSelected(awaitingFirstResult = true, selectedTab = MobileTab.SEARCH))
    }

    @Test
    fun appendingLaterPagesNeverNavigates() {
        val afterFirstPage = searchNavigationAfterCompletion(awaitingFirstResult = true, currentTab = MobileTab.SEARCH)
        assertFalse(afterFirstPage.awaitingFirstResult)

        val afterAppend = searchNavigationAfterCompletion(
            awaitingFirstResult = afterFirstPage.awaitingFirstResult,
            currentTab = MobileTab.RESULTS,
        )

        assertEquals(MobileTab.RESULTS, afterAppend.targetTab)
        assertFalse(afterAppend.awaitingFirstResult)
    }

    @Test
    fun returningToTheSearchPageDoesNotJumpAgain() {
        val afterFirstPage = searchNavigationAfterCompletion(awaitingFirstResult = true, currentTab = MobileTab.SEARCH)

        val back = searchNavigationAfterCompletion(
            awaitingFirstResult = afterFirstPage.awaitingFirstResult,
            currentTab = MobileTab.SEARCH,
        )

        assertEquals(MobileTab.SEARCH, back.targetTab)
    }

    @Test
    fun leavingReadyResetsTheSelectedTabToSearch() {
        assertEquals(
            MobileTab.RECOMMENDATIONS,
            mobileTabAfterPhaseChange(MobileTab.RECOMMENDATIONS, ConnectionPhase.READY),
        )
        assertEquals(
            MobileTab.SEARCH,
            mobileTabAfterPhaseChange(MobileTab.DEVICE, ConnectionPhase.DISCOVERY),
        )
    }
}
