package com.zvec.lanviewer.wear.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.wear.compose.material.Colors
import androidx.wear.compose.material.LocalContentColor
import androidx.wear.compose.material.MaterialTheme

internal object WearTokens {
    val ScreenHorizontal = 28.dp
    val ScreenVertical = 20.dp
    val Space4 = 4.dp
    val Space8 = 8.dp
    val Space12 = 12.dp
    val Space16 = 16.dp
    val Radius12 = 12.dp
}

private val DarkPalette = Colors(
    primary = Color(0xFFD2E5D8),
    primaryVariant = Color(0xFF9FCBB0),
    secondary = Color(0xFFC2D1C7),
    secondaryVariant = Color(0xFF53645A),
    onPrimary = Color(0xFF17211B),
    background = Color(0xFF080A09),
    onBackground = Color(0xFFE8ECE9),
    surface = Color(0xFF1A1E1B),
    onSurface = Color(0xFFE8ECE9),
    onSurfaceVariant = Color(0xFFBDC7C0),
    onSecondary = Color(0xFF1F2923),
    error = Color(0xFFFFB4AB),
    onError = Color(0xFF690005),
)

private val LightPalette = Colors(
    primary = Color(0xFF355E48),
    primaryVariant = Color(0xFF254A37),
    secondary = Color(0xFF53645A),
    secondaryVariant = Color(0xFFCBD8CF),
    onPrimary = Color.White,
    background = Color(0xFFF7F9F7),
    onBackground = Color(0xFF171A18),
    surface = Color(0xFFE8ECE9),
    onSurface = Color(0xFF171A18),
    onSurfaceVariant = Color(0xFF465049),
    onSecondary = Color.White,
    error = Color(0xFFBA1A1A),
    onError = Color.White,
)

@Composable
fun ZvecWearTheme(content: @Composable () -> Unit) {
    val colors = if (isSystemInDarkTheme()) DarkPalette else LightPalette
    MaterialTheme(
        colors = colors,
    ) {
        CompositionLocalProvider(
            LocalContentColor provides colors.onBackground,
            content = content,
        )
    }
}
