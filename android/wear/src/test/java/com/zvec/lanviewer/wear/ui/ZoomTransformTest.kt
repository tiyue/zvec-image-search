package com.zvec.lanviewer.wear.ui

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.unit.IntSize
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ZoomTransformTest {
    @Test
    fun zoomAndPanAreBoundedAndZoomingOutResetsTheImage() {
        val enlarged = updatedZoomTransform(
            current = ZoomTransform(),
            zoomChange = 10f,
            panChange = Offset(2_000f, -2_000f),
            viewport = IntSize(450, 450),
        )

        assertEquals(5f, enlarged.scale)
        assertEquals(Offset(900f, -900f), enlarged.offset)

        val reset = updatedZoomTransform(
            current = enlarged,
            zoomChange = 0.01f,
            panChange = Offset.Zero,
            viewport = IntSize(450, 450),
        )

        assertEquals(ZoomTransform(), reset)
    }

    @Test
    fun horizontalSwipeSelectsTheExpectedDirectionAndRejectsShortOrVerticalMoves() {
        val viewport = IntSize(450, 450)

        assertEquals(
            OriginalNavigation.NEXT,
            originalNavigationForSwipe(Offset(-120f, 10f), viewport),
        )
        assertEquals(
            OriginalNavigation.PREVIOUS,
            originalNavigationForSwipe(Offset(120f, -10f), viewport),
        )
        assertNull(originalNavigationForSwipe(Offset(-60f, 0f), viewport))
        assertNull(originalNavigationForSwipe(Offset(-120f, 140f), viewport))
    }
}
