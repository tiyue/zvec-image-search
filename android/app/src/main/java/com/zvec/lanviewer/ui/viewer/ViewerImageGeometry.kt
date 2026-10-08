package com.zvec.lanviewer.ui.viewer

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * Real Fit geometry for the original-image viewer.
 *
 * [displayWidth] and [displayHeight] are the px size the rotated image occupies on screen at 1x.
 * [contentWidth] and [contentHeight] are the px size of the unrotated composable the image is drawn
 * into, so rotating the layer keeps the whole picture inside the viewport.
 */
internal data class ViewerImageLayout(
    val displayWidth: Float,
    val displayHeight: Float,
    val contentWidth: Float,
    val contentHeight: Float,
)

internal data class ViewerPanLimits(val maxX: Float, val maxY: Float)

/** Normalized rotation in [0, 360). */
internal fun normalizeViewerRotation(degrees: Float): Float {
    val normalized = degrees % 360f
    return if (normalized < 0f) normalized + 360f else normalized
}

/** True for the quarter turns that swap the image bounding box axes. */
internal fun viewerRotationSwapsAxes(degrees: Float): Boolean {
    val normalized = normalizeViewerRotation(degrees)
    return abs(normalized - 90f) < 0.5f || abs(normalized - 270f) < 0.5f
}

/** Intrinsic image size after the 0/90/180/270 rotation. */
internal fun rotatedIntrinsicSize(imageWidth: Int, imageHeight: Int, degrees: Float): Pair<Int, Int> =
    if (viewerRotationSwapsAxes(degrees)) imageHeight to imageWidth else imageWidth to imageHeight

/**
 * Fit layout for an image with a known intrinsic size. Returns null while the viewport or the
 * intrinsic size is unknown so callers keep a 1x centered placeholder instead of guessing the
 * aspect ratio.
 */
internal fun viewerImageLayout(
    viewportWidth: Int,
    viewportHeight: Int,
    imageWidth: Int,
    imageHeight: Int,
    rotationDegrees: Float,
): ViewerImageLayout? {
    if (viewportWidth <= 0 || viewportHeight <= 0 || imageWidth <= 0 || imageHeight <= 0) return null
    val (rotatedWidth, rotatedHeight) = rotatedIntrinsicSize(imageWidth, imageHeight, rotationDegrees)
    val fitScale = min(
        viewportWidth.toFloat() / rotatedWidth.toFloat(),
        viewportHeight.toFloat() / rotatedHeight.toFloat(),
    )
    val displayWidth = rotatedWidth * fitScale
    val displayHeight = rotatedHeight * fitScale
    return if (viewerRotationSwapsAxes(rotationDegrees)) {
        ViewerImageLayout(displayWidth, displayHeight, contentWidth = displayHeight, contentHeight = displayWidth)
    } else {
        ViewerImageLayout(displayWidth, displayHeight, contentWidth = displayWidth, contentHeight = displayHeight)
    }
}

/**
 * Maximum pan distance in px along one axis. It uses the actually displayed Fit size rather than the
 * whole viewport: a letterboxed axis has no slack until the image is zoomed past the viewport.
 */
internal fun viewerPanLimit(viewportSize: Float, displaySize: Float, scale: Float): Float =
    max(0f, (displaySize * scale - viewportSize) / 2f)

internal fun clampViewerPan(value: Float, limit: Float): Float = value.coerceIn(-limit, limit)

/** Pan limits in screen space for the current rotation and zoom, or null while the size is unknown. */
internal fun viewerPanLimits(
    viewportWidth: Int,
    viewportHeight: Int,
    imageWidth: Int,
    imageHeight: Int,
    rotationDegrees: Float,
    scale: Float,
): ViewerPanLimits? {
    val layout = viewerImageLayout(viewportWidth, viewportHeight, imageWidth, imageHeight, rotationDegrees)
        ?: return null
    return ViewerPanLimits(
        maxX = viewerPanLimit(viewportWidth.toFloat(), layout.displayWidth, scale),
        maxY = viewerPanLimit(viewportHeight.toFloat(), layout.displayHeight, scale),
    )
}
