package com.zvec.lanviewer.ui.search

import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.zvec.lanviewer.ui.QueryImageUiState
import com.zvec.lanviewer.ui.formatBytes

internal const val QUERY_IMAGE_PREVIEW_TAG = "query-image-preview"

/**
 * Query-image upload feedback. The four states are distinct: uploading, completed (only with a
 * server id), failed (with the reason and a retry) and attached-but-not-uploaded. The small preview
 * only reads the already selected URI, so showing it never re-uploads the picture.
 */
@Composable
internal fun QueryImageControl(
    query: QueryImageUiState?,
    onPickImage: () -> Unit,
    onCancelUpload: () -> Unit,
    onRetryUpload: () -> Unit,
    onClearImage: () -> Unit,
) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.fillMaxWidth().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (query == null) {
                OutlinedButton(onClick = onPickImage, modifier = Modifier.fillMaxWidth()) {
                    Text("选择查询图片（不限文件大小）")
                }
            } else {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    QueryImagePreview(uri = query.uri)
                    Text(
                        query.displayName,
                        modifier = Modifier.weight(1f),
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    TextButton(onClick = onPickImage) { Text("替换") }
                    TextButton(onClick = if (query.uploading) onCancelUpload else onClearImage) {
                        Text(if (query.uploading) "取消" else "移除")
                    }
                }
                QueryImageStatus(query = query, onRetryUpload = onRetryUpload)
            }
        }
    }
}

@Composable
private fun QueryImagePreview(uri: Uri) {
    Box(
        modifier = Modifier
            .size(48.dp)
            .clip(RoundedCornerShape(6.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .testTag(QUERY_IMAGE_PREVIEW_TAG),
        contentAlignment = Alignment.Center,
    ) {
        AsyncImage(
            model = uri,
            contentDescription = "查询图片预览",
            contentScale = ContentScale.Fit,
            modifier = Modifier.fillMaxSize(),
        )
    }
}

@Composable
private fun QueryImageStatus(query: QueryImageUiState, onRetryUpload: () -> Unit) {
    when {
        query.uploading -> {
            val fraction = query.totalBytes?.takeIf { it > 0 }?.let {
                (query.bytesUploaded.toFloat() / it.toFloat()).coerceIn(0f, 1f)
            }
            if (fraction == null) {
                LinearProgressIndicator(Modifier.fillMaxWidth())
            } else {
                LinearProgressIndicator(progress = { fraction }, modifier = Modifier.fillMaxWidth())
            }
            Text(
                if (query.totalBytes == null) {
                    "上传中 · 已上传 ${formatBytes(query.bytesUploaded)}"
                } else {
                    "上传中 · ${formatBytes(query.bytesUploaded)} / ${formatBytes(query.totalBytes)}"
                },
                style = MaterialTheme.typography.bodySmall,
            )
        }
        query.queryImageId != null -> Text(
            "上传完成",
            color = MaterialTheme.colorScheme.primary,
            style = MaterialTheme.typography.bodySmall,
        )
        query.uploadError != null -> {
            Text(
                "上传失败：${query.uploadError}",
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
            )
            TextButton(onClick = onRetryUpload) { Text("重试") }
        }
        else -> {
            Text("尚未上传", style = MaterialTheme.typography.bodySmall)
            TextButton(onClick = onRetryUpload) { Text("重试") }
        }
    }
}
