package com.zvec.lanviewer.wear.ui

import org.junit.Assert.assertEquals
import org.junit.Test

class OriginalSaverTest {
    @Test
    fun displayNameDropsDirectoriesAndReplacesInvalidCharacters() {
        assertEquals(
            "portrait_.jpg",
            savedOriginalDisplayName("C:\\private\\portrait?.jpg", 123L),
        )
        assertEquals("YaoLens-123.jpg", savedOriginalDisplayName("   ", 123L))
    }
}
