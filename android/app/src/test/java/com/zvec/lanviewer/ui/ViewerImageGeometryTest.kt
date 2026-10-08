package com.zvec.lanviewer.ui

import com.zvec.lanviewer.ui.viewer.clampViewerPan
import com.zvec.lanviewer.ui.viewer.normalizeViewerRotation
import com.zvec.lanviewer.ui.viewer.rotatedIntrinsicSize
import com.zvec.lanviewer.ui.viewer.viewerImageLayout
import com.zvec.lanviewer.ui.viewer.viewerPanLimit
import com.zvec.lanviewer.ui.viewer.viewerPanLimits
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Real Fit/rotation/zoom bounds used by the viewer. These cover the cases the old
 * viewport*(scale-1) formula got wrong: letterboxed axes, quarter turns and 1x reset.
 */
class ViewerImageGeometryTest {

    @Test
    fun landscapeImageFitsWidthAndIsLetterboxedVertically() {
        val layout = requireNotNull(viewerImageLayout(1000, 2000, 4000, 3000, 0f))
        assertEquals(1000f, layout.displayWidth, 0.01f)
        assertEquals(750f, layout.displayHeight, 0.01f)
        assertEquals(layout.displayWidth, layout.contentWidth, 0.01f)
        assertEquals(layout.displayHeight, layout.contentHeight, 0.01f)
    }

    @Test
    fun portraitImageFitsHeight() {
        val layout = requireNotNull(viewerImageLayout(1000, 2000, 3000, 6000, 0f))
        assertEquals(1000f, layout.displayWidth, 0.01f)
        assertEquals(2000f, layout.displayHeight, 0.01f)
    }

    @Test
    fun longImageStaysInsideTheViewport() {
        val layout = requireNotNull(viewerImageLayout(1080, 1920, 1000, 8000, 0f))
        assertEquals(1920f, layout.displayHeight, 0.01f)
        assertTrue(layout.displayWidth <= 1080f)
    }

    @Test
    fun unknownOrEmptySizeReturnsNull() {
        assertNull(viewerImageLayout(0, 2000, 4000, 3000, 0f))
        assertNull(viewerImageLayout(1000, 0, 4000, 3000, 0f))
        assertNull(viewerImageLayout(1000, 2000, 0, 3000, 0f))
        assertNull(viewerImageLayout(1000, 2000, 4000, 0, 0f))
        assertNull(viewerPanLimits(0, 2000, 4000, 3000, 0f, 2f))
    }

    @Test
    fun quarterTurnKeepsTheRotatedAspectAndSwapsTheContentBox() {
        val turned = requireNotNull(viewerImageLayout(1000, 2000, 4000, 3000, 90f))
        // Rotated intrinsic 3000x4000 -> the 90 degrees display keeps the rotated aspect.
        assertEquals(3000f / 4000f, turned.displayWidth / turned.displayHeight, 0.001f)
        assertEquals(turned.displayHeight, turned.contentWidth, 0.01f)
        assertEquals(turned.displayWidth, turned.contentHeight, 0.01f)
    }

    @Test
    fun rotatedIntrinsicSizeSwapsOnlyQuarterTurns() {
        assertEquals(4000 to 3000, rotatedIntrinsicSize(4000, 3000, 0f))
        assertEquals(3000 to 4000, rotatedIntrinsicSize(4000, 3000, 90f))
        assertEquals(4000 to 3000, rotatedIntrinsicSize(4000, 3000, 180f))
        assertEquals(3000 to 4000, rotatedIntrinsicSize(4000, 3000, 270f))
    }

    @Test
    fun everyRotationFitsTheViewportAndCannotPanAtOneTimes() {
        val viewportWidth = 1080
        val viewportHeight = 1920
        listOf(0f, 90f, 180f, 270f).forEach { rotation ->
            val layout = requireNotNull(viewerImageLayout(viewportWidth, viewportHeight, 3000, 4000, rotation))
            assertTrue("rotation $rotation width", layout.displayWidth <= viewportWidth + 0.01f)
            assertTrue("rotation $rotation height", layout.displayHeight <= viewportHeight + 0.01f)
            val limits = requireNotNull(viewerPanLimits(viewportWidth, viewportHeight, 3000, 4000, rotation, 1f))
            assertEquals(0f, limits.maxX, 0.01f)
            assertEquals(0f, limits.maxY, 0.01f)
        }
    }

    @Test
    fun panLimitUsesTheDisplayedImageInsteadOfTheWholeViewport() {
        // 4000x1000 in a 1080x1920 viewport is letterboxed vertically: even at 2x the image height
        // does not reach the viewport edge, so vertical panning must stay at zero.
        val limits = requireNotNull(viewerPanLimits(1080, 1920, 4000, 1000, 0f, 2f))
        assertEquals(540f, limits.maxX, 0.01f)
        assertEquals(0f, limits.maxY, 0.01f)
        assertEquals(0f, viewerPanLimit(1920f, 270f, 2f), 0.01f)
        assertEquals(540f, viewerPanLimit(1080f, 1080f, 2f), 0.01f)
    }

    @Test
    fun panAxesFollowTheRotation() {
        val upright = requireNotNull(viewerPanLimits(1000, 2000, 4000, 1000, 0f, 2f))
        assertEquals(500f, upright.maxX, 0.01f)
        assertEquals(0f, upright.maxY, 0.01f)
        val turned = requireNotNull(viewerPanLimits(1000, 2000, 4000, 1000, 90f, 2f))
        assertEquals(0f, turned.maxX, 0.01f)
        assertEquals(1000f, turned.maxY, 0.01f)
    }

    @Test
    fun clampViewerPanBoundsBothDirectionsAndKeepsZeroLimit() {
        assertEquals(50f, clampViewerPan(50f, 100f), 0.001f)
        assertEquals(-100f, clampViewerPan(-500f, 100f), 0.001f)
        assertEquals(100f, clampViewerPan(500f, 100f), 0.001f)
        assertEquals(0f, clampViewerPan(10f, 0f), 0.001f)
    }

    @Test
    fun rotationNormalizationHandlesNegativeAndWrappedAngles() {
        assertEquals(0f, normalizeViewerRotation(0f), 0.001f)
        assertEquals(270f, normalizeViewerRotation(-90f), 0.001f)
        assertEquals(90f, normalizeViewerRotation(450f), 0.001f)
        assertEquals(180f, normalizeViewerRotation(180f), 0.001f)
    }
}
