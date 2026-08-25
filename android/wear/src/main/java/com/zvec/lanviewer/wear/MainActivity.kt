package com.zvec.lanviewer.wear

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.core.view.WindowCompat
import com.zvec.lanviewer.wear.ui.WatchApp
import com.zvec.lanviewer.wear.ui.WatchViewModel
import com.zvec.lanviewer.wear.ui.WatchViewModelFactory

class MainActivity : ComponentActivity() {
    private val viewModel: WatchViewModel by viewModels {
        val container = (application as ZvecWearApplication).container
        WatchViewModelFactory(
            container.repository,
            container.thumbnailLoader,
            container.originalLoader,
            container.originalSaver,
        )
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        setContent { WatchApp(viewModel) }
    }
}
