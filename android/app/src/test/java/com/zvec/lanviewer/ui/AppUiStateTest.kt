package com.zvec.lanviewer.ui

import com.zvec.lanviewer.data.model.SearchItem
import kotlinx.collections.immutable.persistentListOf
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Test

class AppUiStateTest {
    @Test
    fun resultPagesAppendIntoPersistentListWithoutMutatingPreviousState() {
        val first = SearchItem(mediaId = "m1", name = "one.jpg")
        val second = SearchItem(mediaId = "m2", name = "two.jpg")
        val previous = AppUiState(results = persistentListOf(first))

        val updated = previous.copy(results = previous.results.addAll(listOf(second)))

        assertEquals(listOf(first), previous.results)
        assertEquals(listOf(first, second), updated.results)
        assertSame(first, updated.results.first())
    }

    @Test
    fun pageSizeUsesServerMaximumToReduceRoundTrips() {
        assertEquals(100, SEARCH_PAGE_SIZE)
    }
}
