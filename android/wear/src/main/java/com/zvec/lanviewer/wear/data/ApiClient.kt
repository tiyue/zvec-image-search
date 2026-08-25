package com.zvec.lanviewer.wear.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.Call
import okhttp3.Callback
import okhttp3.Dispatcher
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.net.Proxy
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

class ApiException(
    val httpStatus: Int,
    val errorCode: String,
    override val message: String,
) : IOException(message)

private class AuthInterceptor(
    private val tokenStore: TokenStore,
    private val connectionReader: ConnectionReader,
) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val selected = connectionReader.read()?.baseUrl?.toHttpUrlOrNull()
        val sameOrigin = selected != null &&
            request.url.scheme == selected.scheme &&
            request.url.host == selected.host &&
            request.url.port == selected.port
        val pairingRoute = request.url.encodedPath.startsWith("/api/v1/pair-requests")
        val builder = request.newBuilder().removeHeader("Authorization")
        if (sameOrigin && !pairingRoute) {
            tokenStore.read()?.takeIf(String::isNotBlank)?.let { token ->
                builder.header("Authorization", "Bearer $token")
            }
        }
        return chain.proceed(builder.build())
    }
}

@OptIn(ExperimentalSerializationApi::class)
class ApiClient(
    private val connectionReader: ConnectionReader,
    tokenStore: TokenStore,
) {
    val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = true
    }

    val httpClient: OkHttpClient
    private val pairingClient: OkHttpClient

    init {
        val dispatcher = Dispatcher().apply {
            maxRequests = 8
            maxRequestsPerHost = 8
        }
        httpClient = OkHttpClient.Builder()
            .dispatcher(dispatcher)
            .proxy(Proxy.NO_PROXY)
            .connectTimeout(12, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .callTimeout(0, TimeUnit.MILLISECONDS)
            .retryOnConnectionFailure(true)
            .followRedirects(false)
            .followSslRedirects(false)
            .addInterceptor(AuthInterceptor(tokenStore, connectionReader))
            .build()
        pairingClient = httpClient.newBuilder()
            .retryOnConnectionFailure(false)
            .readTimeout(15, TimeUnit.SECONDS)
            .build()
    }

    suspend fun startPairing(baseUrl: String, request: PairRequest): PairStartResponse {
        val wireRequest = Request.Builder()
            .url(endpoint(baseUrl, "api", "v1", "pair-requests"))
            .post(json.encodeToString(request).jsonBody())
            .build()
        return executePairingJson(wireRequest)
    }

    suspend fun pollPairing(
        baseUrl: String,
        pairingId: String,
        clientSecret: String,
    ): PairPollResponse {
        val wireRequest = Request.Builder()
            .url(endpoint(baseUrl, "api", "v1", "pair-requests", pairingId, "poll"))
            .post(json.encodeToString(PairPollRequest(clientSecret)).jsonBody())
            .build()
        return executePairingJson(wireRequest)
    }

    suspend fun watchRecommendations(request: RecommendationRequest): RecommendationsResponse {
        val selectedBaseUrl = connectionReader.read()?.baseUrl?.toHttpUrl()
            ?: throw IllegalStateException("尚未设置服务器地址")
        val response = executeJson<RecommendationsResponse>(
            Request.Builder()
                .url(endpoint(selectedBaseUrl.toString(), "api", "v1", "watch", "recommendations"))
                .post(json.encodeToString(request).jsonBody())
                .build(),
        )
        return response.copy(
            items = response.items.map { item ->
                item.copy(
                    thumbnailUrl = rebaseMediaUrl(item.thumbnailUrl, selectedBaseUrl),
                    previewUrl = rebaseMediaUrl(item.previewUrl, selectedBaseUrl),
                )
            },
        )
    }

    suspend fun markShown(batchId: String, request: RecommendationShownRequest) {
        executeUnit(
            Request.Builder()
                .url(currentEndpoint("api", "v1", "recommendations", batchId, "shown"))
                .post(json.encodeToString(request).jsonBody())
                .build(),
        )
    }

    suspend fun recordOpen(batchId: String, request: RecommendationActionRequest) {
        executeUnit(
            Request.Builder()
                .url(currentEndpoint("api", "v1", "recommendations", batchId, "actions"))
                .post(json.encodeToString(request).jsonBody())
                .build(),
        )
    }

    suspend fun recordSave(batchId: String, request: RecommendationActionRequest) {
        executeUnit(
            Request.Builder()
                .url(currentEndpoint("api", "v1", "recommendations", batchId, "actions"))
                .post(json.encodeToString(request).jsonBody())
                .build(),
        )
    }

    private fun currentEndpoint(vararg segments: String): HttpUrl {
        val baseUrl = connectionReader.read()?.baseUrl
            ?: throw IllegalStateException("尚未设置服务器地址")
        return endpoint(baseUrl, *segments)
    }

    private fun endpoint(baseUrl: String, vararg segments: String): HttpUrl {
        val builder = baseUrl.toHttpUrl().newBuilder().encodedPath("/")
        segments.forEach(builder::addPathSegment)
        return builder.build()
    }

    private fun rebaseMediaUrl(rawUrl: String, selectedBaseUrl: HttpUrl): String {
        val source = rawUrl.toHttpUrlOrNull()
            ?: throw IOException("服务器返回了无效图片地址")
        return selectedBaseUrl.newBuilder()
            .encodedPath(source.encodedPath)
            .encodedQuery(source.encodedQuery)
            .fragment(null)
            .build()
            .toString()
    }

    private suspend inline fun <reified T> executeJson(
        request: Request,
        client: OkHttpClient = httpClient,
    ): T {
        val response = client.newCall(request).awaitResponse()
        return withContext(Dispatchers.IO) {
            response.use {
                if (!it.isSuccessful) throw decodeError(it)
                val body = it.body?.string() ?: throw IOException("服务器返回了空响应")
                try {
                    json.decodeFromString<T>(body)
                } catch (error: Exception) {
                    throw IOException("服务器响应格式不兼容", error)
                }
            }
        }
    }

    private suspend inline fun <reified T> executePairingJson(request: Request): T = try {
        executeJson(request, pairingClient)
    } catch (error: ApiException) {
        throw error
    } catch (_: IOException) {
        executeJson(request, pairingClient)
    }

    private suspend fun executeUnit(request: Request) {
        val response = httpClient.newCall(request).awaitResponse()
        withContext(Dispatchers.IO) {
            response.use {
                if (!it.isSuccessful) throw decodeError(it)
            }
        }
    }

    private fun decodeError(response: Response): ApiException {
        val raw = response.body?.byteStream()?.use { stream ->
            val buffer = ByteArray(MAX_ERROR_BYTES)
            val count = stream.read(buffer).coerceAtLeast(0)
            String(buffer, 0, count, Charsets.UTF_8)
        }.orEmpty()
        val error = runCatching { json.decodeFromString<ApiErrorEnvelope>(raw) }
            .getOrNull()
            ?.error
        return ApiException(
            httpStatus = response.code,
            errorCode = error?.code?.takeIf(String::isNotBlank) ?: "http_${response.code}",
            message = error?.message?.takeIf(String::isNotBlank)
                ?: "请求失败（HTTP ${response.code}）",
        )
    }

    private fun String.jsonBody() = toRequestBody(JSON_MEDIA_TYPE)

    companion object {
        private const val MAX_ERROR_BYTES = 64 * 1024
        private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
    }
}

private suspend fun Call.awaitResponse(): Response =
    suspendCancellableCoroutine { continuation ->
        continuation.invokeOnCancellation { cancel() }
        enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (continuation.isActive) continuation.resumeWithException(e)
            }

            override fun onResponse(call: Call, response: Response) {
                if (continuation.isActive) continuation.resume(response) else response.close()
            }
        })
    }
