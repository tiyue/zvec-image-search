package com.zvec.lanviewer

import android.content.Context
import com.zvec.lanviewer.data.discovery.UdpDiscoveryService
import com.zvec.lanviewer.data.local.ConnectionStore
import com.zvec.lanviewer.data.local.InstallationStore
import com.zvec.lanviewer.data.network.LanApiClient
import com.zvec.lanviewer.data.network.ResumableDownloader
import com.zvec.lanviewer.data.repository.ZvecRepository
import com.zvec.lanviewer.data.security.KeystoreTokenStore

class AppContainer(context: Context) {
    private val appContext = context.applicationContext
    val connectionStore = ConnectionStore(appContext)
    val tokenStore = KeystoreTokenStore(appContext)
    val apiClient = LanApiClient(
        connectionReader = connectionStore,
        tokenStore = tokenStore,
        maxConcurrentRequests = LanApiClient.DEFAULT_CONCURRENCY,
    )
    val repository = ZvecRepository(
        context = appContext,
        discovery = UdpDiscoveryService(apiClient.json),
        connectionStore = connectionStore,
        installationStore = InstallationStore(appContext),
        tokenStore = tokenStore,
        api = apiClient,
        downloader = ResumableDownloader(apiClient.httpClient),
    )
}
