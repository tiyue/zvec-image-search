package com.zvec.lanviewer.data.local

import android.content.Context
import com.zvec.lanviewer.data.model.DiscoveredServer
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

data class SavedConnection(
    val baseUrl: String,
    val instanceId: String?,
    val displayName: String?,
)

fun interface ConnectionReader {
    fun read(): SavedConnection?
}

class ConnectionStore(context: Context) : ConnectionReader {
    private val preferences = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    override fun read(): SavedConnection? {
        val baseUrl = preferences.getString(KEY_BASE_URL, null) ?: return null
        return SavedConnection(
            baseUrl = baseUrl,
            instanceId = preferences.getString(KEY_INSTANCE_ID, null),
            displayName = preferences.getString(KEY_DISPLAY_NAME, null),
        )
    }

    fun write(connection: SavedConnection) {
        preferences.edit()
            .putString(KEY_BASE_URL, connection.baseUrl)
            .putString(KEY_INSTANCE_ID, connection.instanceId)
            .putString(KEY_DISPLAY_NAME, connection.displayName)
            .apply()
    }

    fun clear() {
        preferences.edit().clear().apply()
    }

    companion object {
        private const val PREFS_NAME = "zvec_lan_connection"
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_INSTANCE_ID = "instance_id"
        private const val KEY_DISPLAY_NAME = "display_name"
    }
}

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
