package com.zvec.lanviewer

import android.app.Application
import coil.ImageLoader
import coil.ImageLoaderFactory
import coil.decode.GifDecoder
import coil.decode.ImageDecoderDecoder
import coil.disk.DiskCache

class ZvecApplication : Application(), ImageLoaderFactory {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(this)
    }

    override fun newImageLoader(): ImageLoader = ImageLoader.Builder(this)
        .okHttpClient(container.apiClient.httpClient)
        .components {
            if (android.os.Build.VERSION.SDK_INT >= 28) add(ImageDecoderDecoder.Factory())
            else add(GifDecoder.Factory())
        }
        .diskCache {
            DiskCache.Builder()
                .directory(cacheDir.resolve("coil-originals"))
                .maxSizePercent(0.20)
                .build()
        }
        .crossfade(true)
        .respectCacheHeaders(true)
        .build()
}
