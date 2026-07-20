package com.zvec.lanviewer.data.discovery

import android.util.Base64
import com.zvec.lanviewer.data.model.DiscoveredServer
import com.zvec.lanviewer.data.model.DiscoveryResponse
import com.zvec.lanviewer.data.local.ServerAddress
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.SocketTimeoutException
import java.security.SecureRandom

class UdpDiscoveryService(
    private val json: Json = Json { ignoreUnknownKeys = true },
) {
    suspend fun discover(timeoutMillis: Long = DEFAULT_TIMEOUT_MS): List<DiscoveredServer> =
        withContext(Dispatchers.IO) {
            require(timeoutMillis in 100L..10_000L)
            val nonce = newNonce()
            val payload = "$REQUEST_PREFIX $nonce".toByteArray(Charsets.UTF_8)
            val deadlineNanos = System.nanoTime() + timeoutMillis * 1_000_000L
            val found = linkedMapOf<String, DiscoveredServer>()

            DatagramSocket(null).use { socket ->
                socket.reuseAddress = true
                socket.broadcast = true
                socket.bind(InetSocketAddress(0))
                socket.soTimeout = RECEIVE_SLICE_MS

                broadcastAddresses().forEach { address ->
                    runCatching {
                        socket.send(DatagramPacket(payload, payload.size, address, DISCOVERY_PORT))
                    }
                }

                while (System.nanoTime() < deadlineNanos) {
                    val buffer = ByteArray(MAX_DATAGRAM_BYTES)
                    val packet = DatagramPacket(buffer, buffer.size)
                    try {
                        socket.receive(packet)
                        parseResponse(packet.data.copyOf(packet.length), nonce)?.let { response ->
                            val server = DiscoveredServer(
                                instanceId = response.instanceId,
                                name = response.name,
                                host = response.host,
                                port = response.port,
                            )
                            found[server.stableKey] = server
                        }
                    } catch (_: SocketTimeoutException) {
                        // Short receive slices make coroutine cancellation and the overall deadline responsive.
                    }
                }
            }
            found.values.toList()
        }

    fun parseResponse(payload: ByteArray, expectedNonce: String): DiscoveryResponse? {
        if (payload.size > MAX_DATAGRAM_BYTES) return null
        return runCatching {
            json.decodeFromString<DiscoveryResponse>(payload.toString(Charsets.UTF_8))
        }.getOrNull()?.takeIf {
            it.protocol == 1 &&
                it.nonce == expectedNonce &&
                it.instanceId.isNotBlank() &&
                it.name.isNotBlank() &&
                ServerAddress.isPrivateIpv4(it.host) &&
                it.port in 1..65535
        }
    }

    private fun broadcastAddresses(): Set<InetAddress> {
        val addresses = linkedSetOf(InetAddress.getByName("255.255.255.255"))
        runCatching {
            NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { it.interfaceAddresses }
                .mapNotNullTo(addresses) { it.broadcast }
        }
        return addresses
    }

    private fun newNonce(): String {
        val bytes = ByteArray(18).also(SecureRandom()::nextBytes)
        return Base64.encodeToString(bytes, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
    }

    companion object {
        const val DISCOVERY_PORT = 38521
        const val REQUEST_PREFIX = "ZVEC_LAN_DISCOVER/1"
        const val DEFAULT_TIMEOUT_MS = 1_800L
        private const val RECEIVE_SLICE_MS = 200
        private const val MAX_DATAGRAM_BYTES = 8 * 1024
    }
}
