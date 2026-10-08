package com.zvec.lanviewer.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * Black/white/grey Material3 scheme. Secondary containers, surfaces and outlines are set explicitly
 * so navigation, sheets and selected states never fall back to the default purple container roles.
 */
private val ZvecColors = lightColorScheme(
    primary = Color(0xFF171717),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFECECEC),
    onPrimaryContainer = Color(0xFF171717),
    secondary = Color(0xFF5A5A5F),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFE3E3E6),
    onSecondaryContainer = Color(0xFF232326),
    tertiary = Color(0xFF55555A),
    onTertiary = Color.White,
    tertiaryContainer = Color(0xFFE6E6E9),
    onTertiaryContainer = Color(0xFF232326),
    background = Color(0xFFF7F7F8),
    onBackground = Color(0xFF1A1A1B),
    surface = Color.White,
    onSurface = Color(0xFF1A1A1B),
    surfaceVariant = Color(0xFFECEFF3),
    onSurfaceVariant = Color(0xFF44474C),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = Color(0xFFF7F7F8),
    surfaceContainer = Color(0xFFF1F1F3),
    surfaceContainerHigh = Color(0xFFEBEBEE),
    surfaceContainerHighest = Color(0xFFE5E5E9),
    outline = Color(0xFF737377),
    outlineVariant = Color(0xFFC6C6CB),
    error = Color(0xFFB91C1C),
    errorContainer = Color(0xFFF7DAD8),
    onErrorContainer = Color(0xFF410E0B),
)

private val ZvecDarkColors = darkColorScheme(
    primary = Color(0xFFF1F1F1),
    onPrimary = Color(0xFF171717),
    primaryContainer = Color(0xFF303030),
    onPrimaryContainer = Color(0xFFF1F1F1),
    secondary = Color(0xFFC2C2C6),
    onSecondary = Color(0xFF2A2A2E),
    secondaryContainer = Color(0xFF34343A),
    onSecondaryContainer = Color(0xFFE4E4E8),
    tertiary = Color(0xFFC6C6CB),
    onTertiary = Color(0xFF2A2A2E),
    tertiaryContainer = Color(0xFF37373D),
    onTertiaryContainer = Color(0xFFE6E6EA),
    background = Color(0xFF121212),
    onBackground = Color(0xFFECECEC),
    surface = Color(0xFF1C1C1C),
    onSurface = Color(0xFFECECEC),
    surfaceVariant = Color(0xFF2B2D30),
    onSurfaceVariant = Color(0xFFC4C6CC),
    surfaceContainerLowest = Color(0xFF0D0D0D),
    surfaceContainerLow = Color(0xFF181818),
    surfaceContainer = Color(0xFF1E1E1E),
    surfaceContainerHigh = Color(0xFF262626),
    surfaceContainerHighest = Color(0xFF2C2C2C),
    outline = Color(0xFF8E9099),
    outlineVariant = Color(0xFF44474C),
    error = Color(0xFFFFB4AB),
    errorContainer = Color(0xFF5C1A16),
    onErrorContainer = Color(0xFFFFDAD6),
)

@Composable
fun ZvecTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) ZvecDarkColors else ZvecColors,
        typography = MaterialTheme.typography,
        content = content,
    )
}
