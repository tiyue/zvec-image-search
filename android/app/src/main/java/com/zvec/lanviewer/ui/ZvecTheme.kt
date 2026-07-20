package com.zvec.lanviewer.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val ZvecColors = lightColorScheme(
    primary = Color(0xFF0F766E),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFCCFBF1),
    onPrimaryContainer = Color(0xFF134E4A),
    secondary = Color(0xFF475569),
    background = Color(0xFFF8FAFC),
    surface = Color.White,
    surfaceVariant = Color(0xFFE2E8F0),
    error = Color(0xFFB91C1C),
)

@Composable
fun ZvecTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = ZvecColors,
        typography = MaterialTheme.typography,
        content = content,
    )
}
