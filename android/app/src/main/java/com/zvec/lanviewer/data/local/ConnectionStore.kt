package com.zvec.lanviewer.data.local

import android.content.Context
import android.content.SharedPreferences
import com.zvec.lanviewer.data.model.DiscoveredServer
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** How the phone currently reaches the paired desktop. */
enum class ConnectionMode { LAN, REMOTE }

/**
 * One paired desktop plus every endpoint the desktop authenticated for it.
 *
 * The trailing fields are defaulted so the previous 3-argument constructor keeps working and an
 * older saved profile (baseUrl + token only) still loads. [mode] is the user's selected entry;
 * [lanBaseUrl] / [remoteBaseUrl] are only ever written from an authenticated status response.
 */
data class SavedConnection(
    val baseUrl: String,
    val instanceId: String?,
    val displayName: String?,
    val mode: ConnectionMode = ConnectionMode.LAN,
    val lanBaseUrl: String? = null,
    val remoteBaseUrl: String? = null,
    val supportsRemoteMedia: Boolean = false,
) {
    fun endpointFor(mode: ConnectionMode): String? = when (mode) {
        ConnectionMode.LAN -> lanBaseUrl
        ConnectionMode.REMOTE -> remoteBaseUrl
    }
}

fun interface ConnectionReader {
    fun read(): SavedConnection?
}

class ConnectionStore(context: Context) : ConnectionReader {
    private val preferences = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    override fun read(): SavedConnection? {
        val baseUrl = preferences.getString(KEY_BASE_URL, null) ?: return null
        val mode = preferences.getString(KEY_MODE, null)
            ?.let { name -> runCatching { ConnectionMode.valueOf(name) }.getOrNull() }
            ?: classifyLegacyMode(baseUrl)
        return SavedConnection(
            baseUrl = baseUrl,
            instanceId = preferences.getString(KEY_INSTANCE_ID, null),
            displayName = preferences.getString(KEY_DISPLAY_NAME, null),
            mode = mode,
            lanBaseUrl = preferences.getString(KEY_LAN_BASE_URL, null),
            remoteBaseUrl = preferences.getString(KEY_REMOTE_BASE_URL, null),
            supportsRemoteMedia = preferences.getBoolean(KEY_SUPPORTS_REMOTE_MEDIA, false),
        )
    }

    fun write(connection: SavedConnection) {
        preferences.edit()
            .putString(KEY_BASE_URL, connection.baseUrl)
            .putNullable(KEY_INSTANCE_ID, connection.instanceId)
            .putNullable(KEY_DISPLAY_NAME, connection.displayName)
            .putString(KEY_MODE, connection.mode.name)
            .putNullable(KEY_LAN_BASE_URL, connection.lanBaseUrl)
            .putNullable(KEY_REMOTE_BASE_URL, connection.remoteBaseUrl)
            .putBoolean(KEY_SUPPORTS_REMOTE_MEDIA, connection.supportsRemoteMedia)
            .apply()
    }

    fun clear() {
        preferences.edit().clear().apply()
    }

    /** A legacy profile has no stored mode: a public IPv4 entry is treated as REMOTE, otherwise LAN. */
    private fun classifyLegacyMode(baseUrl: String): ConnectionMode {
        val host = baseUrl.toHttpUrlOrNull()?.host ?: return ConnectionMode.LAN
        return if (ServerAddress.isValidIpv4(host) && !ServerAddress.isPrivateIpv4(host)) {
            ConnectionMode.REMOTE
        } else {
            ConnectionMode.LAN
        }
    }

    companion object {
        private const val PREFS_NAME = "zvec_lan_connection"
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_INSTANCE_ID = "instance_id"
        private const val KEY_DISPLAY_NAME = "display_name"
        private const val KEY_MODE = "connection_mode"
        private const val KEY_LAN_BASE_URL = "lan_base_url"
        private const val KEY_REMOTE_BASE_URL = "remote_base_url"
        private const val KEY_SUPPORTS_REMOTE_MEDIA = "supports_remote_media"
    }
}

private fun SharedPreferences.Editor.putNullable(key: String, value: String?): SharedPreferences.Editor =
    if (value == null) remove(key) else putString(key, value)

object ServerAddress {
    const val DEFAULT_PORT = 38522

    fun fromHostPort(hostInput: String, port: Int = DEFAULT_PORT): String? {
        if (port !in 1..65535) return null
        val trimmed = hostInput.trim().trimEnd('/')
        if (trimmed.isBlank()) return null
        val withScheme = if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
            trimmed
        } else {
            "http://$trimmed"
        }
        val parsed = withScheme.toHttpUrlOrNull() ?: return null
        if (parsed.scheme != "http" || !isValidIpv4(parsed.host)) return null
        if (parsed.username.isNotEmpty() || parsed.password.isNotEmpty()) return null
        if (parsed.encodedPath != "/" || parsed.query != null || parsed.fragment != null) return null
        return parsed.newBuilder()
            .port(port)
            .encodedPath("/")
            .query(null)
            .fragment(null)
            .build()
            .toString()
            .trimEnd('/')
    }

    fun fromDiscovery(server: DiscoveredServer): String? = fromHostPort(server.host, server.port)

    /**
     * Validates a full HTTP IPv4 origin (for example `http://39.105.48.52:38522`) while keeping the
     * explicit port it already carries, unlike [fromHostPort] which always overrides the port.
     * Rejects unknown schemes, domains, userinfo, non-root paths, queries and fragments.
     */
    fun parseOrigin(originInput: String): String? {
        val trimmed = originInput.trim()
        if (trimmed.isBlank()) return null
        val parsed = trimmed.toHttpUrlOrNull() ?: return null
        if (parsed.scheme != "http" || !isValidIpv4(parsed.host)) return null
        if (parsed.username.isNotEmpty() || parsed.password.isNotEmpty()) return null
        if (parsed.encodedPath != "/" || parsed.query != null || parsed.fragment != null) return null
        if (parsed.port !in 1..65535) return null
        return parsed.newBuilder()
            .encodedPath("/")
            .query(null)
            .fragment(null)
            .build()
            .toString()
            .trimEnd('/')
    }

    /** Accepts any well-formed IPv4 literal (private or public) for manual connections. */
    fun isValidIpv4(host: String): Boolean {
        val octets = host.split('.')
        if (octets.size != 4) return false
        return octets.all { part ->
            part.isNotEmpty() && part.length <= 3 && part.all(Char::isDigit) &&
                !(part.length > 1 && part.startsWith('0')) &&
                (part.toIntOrNull() ?: -1) in 0..255
        }
    }

    /** RFC1918-only check, used by UDP discovery to stay within the local network. */
    fun isPrivateIpv4(host: String): Boolean {
        if (!isValidIpv4(host)) return false
        val octets = host.split('.').map { it.toInt() }
        return octets[0] == 10 ||
            (octets[0] == 172 && octets[1] in 16..31) ||
            (octets[0] == 192 && octets[1] == 168)
    }
}
