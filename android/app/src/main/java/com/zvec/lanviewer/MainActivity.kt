package com.zvec.lanviewer

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.lifecycle.viewmodel.compose.viewModel
import com.zvec.lanviewer.ui.AppViewModel
import com.zvec.lanviewer.ui.ZvecApp
import com.zvec.lanviewer.ui.ZvecTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val container = (application as ZvecApplication).container
        setContent {
            ZvecTheme {
                val appViewModel: AppViewModel = viewModel(
                    factory = AppViewModel.Factory(applicationContext, container.repository, container.savedFilesStore),
                )
                ZvecApp(appViewModel)
            }
        }
    }
}
