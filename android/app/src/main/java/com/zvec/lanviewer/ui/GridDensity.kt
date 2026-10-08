package com.zvec.lanviewer.ui

import android.content.Context
import androidx.compose.foundation.layout.Box
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag

internal const val GALLERY_SPACING_DP = 8
internal const val GALLERY_EDGE_PADDING_DP = 8

internal const val DENSITY_ENTRY_TAG = "gallery-density-entry"

internal fun densityOptionTag(density: GalleryDensity): String = "gallery-density-option-${density.name}"

/**
 * Shared phone/tablet thumbnail density for search results and recommendations.
 *
 * Cell widths are tuned against the shared 8dp spacing and 8dp edge padding: a 320dp phone keeps two
 * columns on the standard setting, a 360dp phone gains a column in compact mode, and 600/840dp
 * windows gain clearly more columns. The choice stays a local preference and is never sent to the
 * Windows service.
 */
internal enum class GalleryDensity(val label: String, val targetCellWidthDp: Int) {
    COMPACT("紧凑", 104),
    STANDARD("标准", 144),
    LARGE("大图", 208);

    /** Stable card height per density so the thumbnail box never resizes between load states. */
    val cellHeightDp: Int get() = targetCellWidthDp
}

private const val GALLERY_PREFS = "zvec_gallery_prefs"
private const val GALLERY_DENSITY_KEY = "gallery_density"

/** Local-only density preference kept in the app's own small preferences. */
internal object GalleryDensityStore {
    fun load(context: Context): GalleryDensity {
        val stored = context.applicationContext
            .getSharedPreferences(GALLERY_PREFS, Context.MODE_PRIVATE)
            .getString(GALLERY_DENSITY_KEY, null)
        return GalleryDensity.values().firstOrNull { it.name == stored } ?: GalleryDensity.STANDARD
    }

    fun save(context: Context, density: GalleryDensity) {
        context.applicationContext
            .getSharedPreferences(GALLERY_PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(GALLERY_DENSITY_KEY, density.name)
            .apply()
    }
}

/** Observable density selection that persists every accepted change. */
internal class GalleryDensityPreference(
    private val context: Context,
    initial: GalleryDensity,
) {
    var density by mutableStateOf(initial)
        private set

    fun select(value: GalleryDensity) {
        if (value == density) return
        density = value
        GalleryDensityStore.save(context, value)
    }
}

@Composable
internal fun rememberGalleryDensity(): GalleryDensityPreference {
    val context = LocalContext.current.applicationContext
    return remember(context) { GalleryDensityPreference(context, GalleryDensityStore.load(context)) }
}

/** Compact density entry shared by the results header and the recommendation header. */
@Composable
internal fun GalleryDensityEntry(
    density: GalleryDensity,
    onDensityChange: (GalleryDensity) -> Unit,
    modifier: Modifier = Modifier,
) {
    var expanded by remember { mutableStateOf(false) }
    Box(modifier) {
        TextButton(
            onClick = { expanded = true },
            modifier = Modifier.testTag(DENSITY_ENTRY_TAG),
        ) {
            Text("布局 · ${density.label}")
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            GalleryDensity.values().forEach { option ->
                DropdownMenuItem(
                    text = { Text(option.label) },
                    onClick = {
                        expanded = false
                        onDensityChange(option)
                    },
                    modifier = Modifier.testTag(densityOptionTag(option)),
                )
            }
        }
    }
}
