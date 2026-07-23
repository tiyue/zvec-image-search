package com.zvec.lanviewer.data.local

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

data class SavedFileRecord(
    val name: String,
    val uri: String,
    val savedAtMillis: Long,
    val sizeBytes: Long? = null,
)

class SavedFilesStore(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun add(record: SavedFileRecord) {
        val list = list().toMutableList()
        list.add(0, record)
        val jsonArray = JSONArray()
        list.take(MAX_RECORDS).forEach { jsonArray.put(it.toJson()) }
        prefs.edit().putString(KEY_RECORDS, jsonArray.toString()).apply()
    }

    fun list(): List<SavedFileRecord> = runCatching {
        val raw = prefs.getString(KEY_RECORDS, null) ?: return emptyList()
        val jsonArray = JSONArray(raw)
        (0 until jsonArray.length()).map { jsonArray.getJSONObject(it).toRecord() }
    }.getOrDefault(emptyList())

    private companion object {
        const val PREFS_NAME = "zvec_saved_files"
        const val KEY_RECORDS = "records"
        const val MAX_RECORDS = 200

        fun SavedFileRecord.toJson(): JSONObject = JSONObject().apply {
            put("name", name)
            put("uri", uri)
            put("savedAtMillis", savedAtMillis)
            put("sizeBytes", sizeBytes ?: JSONObject.NULL)
        }

        fun JSONObject.toRecord(): SavedFileRecord = SavedFileRecord(
            name = optString("name", ""),
            uri = optString("uri", ""),
            savedAtMillis = optLong("savedAtMillis", 0L),
            sizeBytes = if (isNull("sizeBytes")) null else optLong("sizeBytes"),
        )
    }
}
