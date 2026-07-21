package com.zvec.lanviewer.data.network

import android.content.ContentResolver
import android.net.Uri
import com.zvec.lanviewer.data.local.ConnectionReader
import com.zvec.lanviewer.data.model.ErrorEnvelope
import com.zvec.lanviewer.data.model.LibrariesResponse
import com.zvec.lanviewer.data.model.PairPollRequest
import com.zvec.lanviewer.data.model.PairPollResponse
import com.zvec.lanviewer.data.model.PairRequest
import com.zvec.lanviewer.data.model.PairStartResponse
import com.zvec.lanviewer.data.model.QueryImageResponse
import com.zvec.lanviewer.data.model.SearchCreatedResponse
import com.zvec.lanviewer.data.model.SearchPageResponse
import com.zvec.lanviewer.data.model.SearchPageResult
import com.zvec.lanviewer.data.model.SearchPendingResponse
import com.zvec.lanviewer.data.model.SearchRequest
import com.zvec.lanviewer.data.model.StatusResponse
import com.zvec.lanviewer.data.model.UploadProgress
import com.zvec.lanviewer.data.security.SecureTokenStore
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
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.net.Proxy
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

@OptIn(ExperimentalSerializationApi::class)
class LanApiClient(
    private val connectionReader: ConnectionReader,
    tokenStore: SecureTokenStore,
    maxConcurrentRequests: Int = DEFAULT_CONCURRENCY,
    val json: Json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = true
    },
) {
    val httpClient: OkHttpClient
    private val pairingHttpClient: OkHttpClient

    init {
        require(maxConcurrentRequests in 1..64) { "并发数必须在 1..64 之间" }
        val dispatcher = Dispatcher().apply {
            maxRequests = maxConcurrentRequests
            maxRequestsPerHost = maxConcurrentRequests
        }
        httpClient = OkHttpClient.Builder()
            .dispatcher(dispatcher)
            // Android may inherit a system/VPN proxy. Private RFC1918 Zvec traffic must
            // stay on the local network and must never depend on that proxy being healthy.
            .proxy(Proxy.NO_PROXY)
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(0, TimeUnit.MILLISECONDS)
            .callTimeout(0, TimeUnit.MILLISECONDS)
            .retryOnConnectionFailure(true)
            .followRedirects(false)
            .followSslRedirects(false)
            .addInterceptor(AuthInterceptor(tokenStore, connectionReader))
            .build()
        // Pairing owns an explicit, exactly-once retry with an identical body. Disable
        // OkHttp's hidden retry only for these calls so production can never exceed 2 sends.
        pairingHttpClient = httpClient.newBuilder()
            .retryOnConnectionFailure(false)
            .readTimeout(8, TimeUnit.SECONDS)
            .build()
    }

    suspend fun startPairing(baseUrl: String, request: PairRequest): PairStartResponse {
        // Serialize once so a response-lost retry is byte-for-byte identical. The desktop
        // treats device_id + client_secret as the idempotency key and returns the original
        // comparison code instead of creating an orphan request with a different code.
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

    suspend fun status(): StatusResponse = executeJson(
        Request.Builder().url(currentEndpoint("api", "v1", "status")).get().build(),
    )

    suspend fun libraries(): LibrariesResponse = executeJson(
        Request.Builder().url(currentEndpoint("api", "v1", "libraries")).get().build(),
    )

    suspend fun uploadQueryImage(
        contentResolver: ContentResolver,
        uri: Uri,
        displayName: String,
        mimeType: String?,
        onProgress: (UploadProgress) -> Unit,
    ): QueryImageResponse {
        val body = ContentUriRequestBody(
            contentResolver = contentResolver,
            uri = uri,
            mediaType = mimeType?.toMediaTypeOrNull(),
            onProgress = onProgress,
        )
        val url = currentEndpoint("api", "v1", "query-images").newBuilder()
            .addQueryParameter("name", displayName)
            .build()
        val request = Request.Builder().url(url).post(body).build()
        return executeJson(request) { body.cancel() }
    }

    suspend fun deleteQueryImage(queryImageId: String) {
        executeUnit(
            Request.Builder()
                .url(currentEndpoint("api", "v1", "query-images", queryImageId))
                .delete()
                .build(),
        )
    }

    suspend fun createSearch(request: SearchRequest): SearchCreatedResponse = executeJson(
        Request.Builder()
            .url(currentEndpoint("api", "v1", "searches"))
            .post(json.encodeToString(request).jsonBody())
            .build(),
    )

    suspend fun searchPage(searchId: String, page: Int, pageSize: Int): SearchPageResult {
        require(page > 0)
        require(pageSize > 0)
        val url = currentEndpoint("api", "v1", "searches", searchId).newBuilder()
            .addQueryParameter("page", page.toString())
            .addQueryParameter("page_size", pageSize.toString())
            .build()
        val response = httpClient.newCall(Request.Builder().url(url).get().build()).awaitResponse()
        return withContext(Dispatchers.IO) {
            response.use {
                if (it.code == 202) {
                    val body = it.body?.string() ?: throw IOException("服务器返回了空的搜索状态")
                    val pending = try {
                        json.decodeFromString<SearchPendingResponse>(body)
                    } catch (error: Exception) {
                        throw IOException("服务器搜索状态格式不兼容", error)
                    }
                    val retryAfter = it.header("Retry-After")?.toLongOrNull()
                        ?: pending.retryAfterSeconds
                    return@withContext SearchPageResult.Pending(
                        status = pending.status,
                        retryAfterSeconds = retryAfter.coerceIn(1L, 30L),
                    )
                }
                if (!it.isSuccessful) throw decodeError(it)
                val body = it.body?.string() ?: throw IOException("服务器返回了空响应")
                val pageResponse = try {
                    json.decodeFromString<SearchPageResponse>(body)
                } catch (error: Exception) {
                    throw IOException("服务器搜索结果格式不兼容", error)
                }
                SearchPageResult.Ready(pageResponse)
            }
        }
    }

    suspend fun deleteSearch(searchId: String) {
        executeUnit(
            Request.Builder()
                .url(currentEndpoint("api", "v1", "searches", searchId))
                .delete()
                .build(),
        )
    }

    suspend fun deleteSession() {
        executeUnit(
            Request.Builder().url(currentEndpoint("api", "v1", "session")).delete().build(),
        )
    }

    fun mediaUrl(mediaId: String): String =
        currentEndpoint("api", "v1", "media", mediaId, "original").toString()

    private fun currentEndpoint(vararg segments: String): HttpUrl {
        val baseUrl = connectionReader.read()?.baseUrl
            ?: throw IllegalStateException("尚未选择 Zvec 电脑")
        return endpoint(baseUrl, *segments)
    }

    private fun endpoint(baseUrl: String, vararg segments: String): HttpUrl {
        val builder = baseUrl.toHttpUrl().newBuilder()
        builder.encodedPath("/")
        segments.forEach(builder::addPathSegment)
        return builder.build()
    }

    private suspend inline fun <reified T> executeJson(
        request: Request,
        client: OkHttpClient = httpClient,
        noinline onCancel: () -> Unit = {},
    ): T {
        val response = client.newCall(request).awaitResponse(onCancel)
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
        executeJson(request, pairingHttpClient)
    } catch (error: ApiException) {
        // A received HTTP response is deterministic; retrying 4xx/5xx can create noise
        // and must not conceal the server's actionable error code.
        throw error
    } catch (_: IOException) {
        // Retry exactly once only when the transport or response body was interrupted.
        // Pairing create/poll are idempotent for the same device secret on the desktop.
        executeJson(request, pairingHttpClient)
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
        val envelope = runCatching { json.decodeFromString<ErrorEnvelope>(raw) }.getOrNull()
        return ApiException(
            httpStatus = response.code,
            errorCode = envelope?.error?.code ?: "http_${response.code}",
            message = envelope?.error?.message ?: "请求失败（HTTP ${response.code}）",
        )
    }

    private fun String.jsonBody() = toRequestBody(JSON_MEDIA_TYPE)

    companion object {
        const val DEFAULT_CONCURRENCY = 10
        private const val MAX_ERROR_BYTES = 64 * 1024
        private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaTypeOrNull()
    }
}

internal suspend fun Call.awaitResponse(onCancel: () -> Unit = {}): Response =
    suspendCancellableCoroutine { continuation ->
        continuation.invokeOnCancellation {
            onCancel()
            cancel()
        }
        enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (continuation.isActive) continuation.resumeWithException(e)
            }

            override fun onResponse(call: Call, response: Response) {
                if (continuation.isActive) {
                    continuation.resume(response)
                } else {
                    response.close()
                }
            }
        })
    }
