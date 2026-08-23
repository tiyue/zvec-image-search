package com.zvec.lanviewer.wear.data

import android.content.Context
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

data class SavedConnection(val baseUrl: String)

data class HostPort(val host: String, val port: Int)

fun interface ConnectionReader {
    fun read(): SavedConnection?
}

class ConnectionStore(context: Context) : ConnectionReader {
    private val preferences = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    override fun read(): SavedConnection? = preferences.getString(KEY_BASE_URL, null)
        ?.let(::SavedConnection)

    fun write(connection: SavedConnection) {
        preferences.edit().putString(KEY_BASE_URL, connection.baseUrl).apply()
    }

    companion object {
        private const val PREFS_NAME = "zvec_wear_connection"
        private const val KEY_BASE_URL = "base_url"
    }
}

object ServerAddress {
    const val DEFAULT_HOST = "39.105.48.52"
    const val DEFAULT_PORT = 38522

    fun fromHostPort(hostInput: String, port: Int): String? {
        if (port !in 1..65535) return null
        val trimmed = hostInput.trim().trimEnd('/')
        if (trimmed.isBlank()) return null
        val withScheme = if (trimmed.startsWith("http://")) trimmed else "http://$trimmed"
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

    fun hostPort(baseUrl: String): HostPort? {
        val parsed = baseUrl.toHttpUrlOrNull() ?: return null
        if (parsed.scheme != "http" || !isValidIpv4(parsed.host)) return null
        return HostPort(parsed.host, parsed.port)
    }

    fun isValidIpv4(host: String): Boolean {
        val parts = host.split('.')
        if (parts.size != 4) return false
        return parts.all { part ->
            part.isNotEmpty() && part.length <= 3 && part.all(Char::isDigit) &&
                !(part.length > 1 && part.startsWith('0')) &&
                (part.toIntOrNull() ?: -1) in 0..255
        }
    }
}
