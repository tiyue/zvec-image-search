package com.zvec.lanviewer.data.local

import android.content.Context
import android.os.Build
import java.util.UUID

class InstallationStore(context: Context) {
    private val preferences = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    @get:Synchronized
    val deviceId: String
        get() = preferences.getString(KEY_DEVICE_ID, null) ?: UUID.randomUUID().toString().also {
            preferences.edit().putString(KEY_DEVICE_ID, it).apply()
        }

    val deviceName: String
        get() = Build.MODEL?.trim()?.takeIf(String::isNotBlank)?.take(80) ?: "Android device"

    companion object {
        private const val PREFS_NAME = "zvec_lan_installation"
        private const val KEY_DEVICE_ID = "device_id"
    }
}
