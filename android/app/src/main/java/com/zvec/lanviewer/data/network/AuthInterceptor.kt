package com.zvec.lanviewer.data.network

import com.zvec.lanviewer.data.local.ConnectionReader
import com.zvec.lanviewer.data.security.SecureTokenStore
import okhttp3.Interceptor
import okhttp3.Response
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** Adds credentials only to the currently selected Zvec origin and never to pairing routes. */
class AuthInterceptor(
    private val tokenStore: SecureTokenStore,
    private val connectionReader: ConnectionReader,
) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val allowedUrl = connectionReader.read()?.baseUrl?.toHttpUrlOrNull()
        val sameOrigin = allowedUrl != null &&
            request.url.scheme == allowedUrl.scheme &&
            request.url.host == allowedUrl.host &&
            request.url.port == allowedUrl.port
        val isPairingRoute = request.url.encodedPath.startsWith("/api/v1/pair-requests")
        val sanitized = request.newBuilder().removeHeader("Authorization")
        if (sameOrigin && !isPairingRoute) {
            tokenStore.read()?.takeIf { it.isNotBlank() }?.let { token ->
                sanitized.header("Authorization", "Bearer $token")
            }
        }
        return chain.proceed(sanitized.build())
    }
}
