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
        if (parsed.scheme != "http" || !isPrivateIpv4(parsed.host)) return null
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

    /** This product deliberately accepts only RFC1918 IPv4 literals, never DNS or public hosts. */
    fun isPrivateIpv4(host: String): Boolean {
        val octets = host.split('.')
        if (octets.size != 4) return false
        val values = octets.map { part ->
            if (part.isEmpty() || part.length > 3 || !part.all(Char::isDigit)) return false
            if (part.length > 1 && part.startsWith('0')) return false
            part.toIntOrNull()?.takeIf { it in 0..255 } ?: return false
        }
        return values[0] == 10 ||
            (values[0] == 172 && values[1] in 16..31) ||
            (values[0] == 192 && values[1] == 168)
    }
}
