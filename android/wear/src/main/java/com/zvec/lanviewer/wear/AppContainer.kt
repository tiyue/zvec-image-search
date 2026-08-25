package com.zvec.lanviewer.wear

import android.content.Context
import coil.ImageLoader
import coil.disk.DiskCache
import com.zvec.lanviewer.wear.data.ApiClient
import com.zvec.lanviewer.wear.data.ConnectionStore
import com.zvec.lanviewer.wear.data.DefaultWatchRepository
import com.zvec.lanviewer.wear.data.InstallationStore
import com.zvec.lanviewer.wear.data.KeystoreTokenStore
import com.zvec.lanviewer.wear.ui.CoilOriginalLoader
import com.zvec.lanviewer.wear.ui.CoilThumbnailLoader
import com.zvec.lanviewer.wear.ui.MediaStoreOriginalSaver

class AppContainer(context: Context) {
    private val appContext = context.applicationContext
    private val connectionStore = ConnectionStore(appContext)
    private val tokenStore = KeystoreTokenStore(appContext)
    private val api = ApiClient(connectionStore, tokenStore)

    val repository = DefaultWatchRepository(
        connectionStore = connectionStore,
        installationStore = InstallationStore(appContext),
        tokenStore = tokenStore,
        api = api,
    )

    val imageLoader: ImageLoader = ImageLoader.Builder(appContext)
        .okHttpClient(api.httpClient)
        .crossfade(true)
        .respectCacheHeaders(false)
        .diskCache {
            DiskCache.Builder()
                .directory(appContext.cacheDir.resolve("wear-image-cache"))
                .maxSizeBytes(128L * 1024L * 1024L)
                .build()
        }
        .build()

    val thumbnailLoader = CoilThumbnailLoader(appContext, imageLoader)
    val originalLoader = CoilOriginalLoader(appContext, imageLoader)
    val originalSaver = MediaStoreOriginalSaver(appContext, imageLoader)
}
