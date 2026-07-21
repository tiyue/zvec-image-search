package com.zvec.lanviewer.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val ZvecColors = lightColorScheme(
    primary = Color(0xFF171717),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFECECEC),
    onPrimaryContainer = Color(0xFF171717),
    secondary = Color(0xFF777777),
    background = Color(0xFFF7F7F8),
    surface = Color.White,
    surfaceVariant = Color(0xFFECEFF3),
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
