package com.zvec.lanviewer.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
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

private val ZvecDarkColors = darkColorScheme(
    primary = Color(0xFFF1F1F1),
    onPrimary = Color(0xFF171717),
    primaryContainer = Color(0xFF303030),
    onPrimaryContainer = Color(0xFFF1F1F1),
    secondary = Color(0xFFB6B6B6),
    background = Color(0xFF121212),
    surface = Color(0xFF1C1C1C),
    surfaceVariant = Color(0xFF2B2D30),
    error = Color(0xFFFFB4AB),
)

@Composable
fun ZvecTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) ZvecDarkColors else ZvecColors,
        typography = MaterialTheme.typography,
        content = content,
    )
}
