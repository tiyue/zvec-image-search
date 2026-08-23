package com.zvec.lanviewer.wear.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.rememberTransformableState
import androidx.compose.foundation.gestures.transformable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.compose.foundation.text.KeyboardOptions
import androidx.wear.compose.material.Chip
import androidx.wear.compose.material.CircularProgressIndicator
import androidx.wear.compose.material.CompactChip
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.Text
import coil.compose.AsyncImage
import coil.compose.SubcomposeAsyncImage
import coil.compose.SubcomposeAsyncImageContent
import coil.request.CachePolicy
import coil.request.ImageRequest
import com.zvec.lanviewer.wear.data.RecommendationItem

@Composable
fun WatchApp(viewModel: WatchViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()

    BackHandler(enabled = state.page == WatchPage.ORIGINAL) {
        viewModel.closeOriginal()
    }
    BackHandler(enabled = state.page == WatchPage.PAIRING) {
        viewModel.cancelPairing()
    }
    BackHandler(
        enabled = state.page == WatchPage.CONNECTION && state.canReturnFromSettings,
    ) {
        viewModel.closeSettings()
    }

    ZvecWearTheme {
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(MaterialTheme.colors.background),
        ) {
            when (state.page) {
                WatchPage.CONNECTION -> ConnectionScreen(
                    state = state,
                    onHostChange = viewModel::updateHost,
                    onPortChange = viewModel::updatePort,
                    onConnect = viewModel::connect,
                    onBack = viewModel::closeSettings,
                )
                WatchPage.PAIRING -> PairingScreen(
                    code = state.comparisonCode.orEmpty(),
                    message = state.pairingMessage.orEmpty(),
                    onCancel = viewModel::cancelPairing,
                )
                WatchPage.RECOMMENDATIONS -> RecommendationsScreen(
                    state = state.recommendations,
                    onRefresh = viewModel::loadRecommendations,
                    onSettings = viewModel::openSettings,
                    onOpen = viewModel::openOriginal,
                )
                WatchPage.ORIGINAL -> state.selectedItem?.let { item ->
                    OriginalScreen(item = item)
                }
            }
        }
    }
}

@Composable
private fun ConnectionScreen(
    state: WatchUiState,
    onHostChange: (String) -> Unit,
    onPortChange: (String) -> Unit,
    onConnect: () -> Unit,
    onBack: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(
                horizontal = WearTokens.ScreenHorizontal,
                vertical = WearTokens.ScreenVertical,
            ),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            text = "连接电脑",
            fontSize = 19.sp,
            fontWeight = FontWeight.SemiBold,
        )
        Spacer(Modifier.height(WearTokens.Space12))
        FilledInput(
            label = "IP",
            value = state.host,
            onValueChange = onHostChange,
            keyboardType = KeyboardType.Uri,
        )
        Spacer(Modifier.height(WearTokens.Space8))
        FilledInput(
            label = "端口",
            value = state.port,
            onValueChange = onPortChange,
            keyboardType = KeyboardType.Number,
        )
        state.connectionError?.let { message ->
            Spacer(Modifier.height(WearTokens.Space8))
            Text(
                text = message,
                color = MaterialTheme.colors.error,
                fontSize = 12.sp,
                textAlign = TextAlign.Center,
            )
        }
        Spacer(Modifier.height(WearTokens.Space12))
        Chip(
            onClick = onConnect,
            enabled = !state.isConnecting,
            label = {
                Text(
                    text = if (state.isConnecting) "正在连接…" else "连接",
                    modifier = Modifier.fillMaxWidth(),
                    textAlign = TextAlign.Center,
                )
            },
            modifier = Modifier.fillMaxWidth(),
        )
        if (state.canReturnFromSettings) {
            Spacer(Modifier.height(WearTokens.Space8))
            CompactChip(
                onClick = onBack,
                label = { Text("返回推荐") },
            )
        }
    }
}

@Composable
private fun FilledInput(
    label: String,
    value: String,
    onValueChange: (String) -> Unit,
    keyboardType: KeyboardType,
) {
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(
            text = label,
            color = MaterialTheme.colors.onBackground.copy(alpha = 0.72f),
            fontSize = 11.sp,
            modifier = Modifier.padding(start = WearTokens.Space4, bottom = WearTokens.Space4),
        )
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
            textStyle = TextStyle(
                color = MaterialTheme.colors.onSurface,
                fontSize = 15.sp,
            ),
            cursorBrush = SolidColor(MaterialTheme.colors.primary),
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(WearTokens.Radius12))
                .background(MaterialTheme.colors.surface)
                .semantics { contentDescription = label }
                .padding(horizontal = WearTokens.Space12, vertical = WearTokens.Space12),
        )
    }
}

@Composable
private fun PairingScreen(
    code: String,
    message: String,
    onCancel: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(
                horizontal = WearTokens.ScreenHorizontal,
                vertical = WearTokens.ScreenVertical,
            ),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(text = "电脑端确认码", fontSize = 14.sp)
        Spacer(Modifier.height(WearTokens.Space8))
        Text(
            text = code,
            fontSize = 26.sp,
            fontWeight = FontWeight.Bold,
            letterSpacing = 3.sp,
        )
        Spacer(Modifier.height(WearTokens.Space8))
        Text(
            text = message,
            color = MaterialTheme.colors.onBackground.copy(alpha = 0.72f),
            fontSize = 12.sp,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(WearTokens.Space12))
        CircularProgressIndicator(modifier = Modifier.size(24.dp))
        Spacer(Modifier.height(WearTokens.Space12))
        CompactChip(onClick = onCancel, label = { Text("取消") })
    }
}

@Composable
private fun RecommendationsScreen(
    state: RecommendationUiState,
    onRefresh: () -> Unit,
    onSettings: () -> Unit,
    onOpen: (String) -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(
                horizontal = WearTokens.ScreenHorizontal,
                vertical = WearTokens.Space12,
            ),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                text = "今日推荐",
                fontSize = 18.sp,
                fontWeight = FontWeight.SemiBold,
            )
            CompactChip(onClick = onSettings, label = { Text("IP") })
        }
        Spacer(Modifier.height(WearTokens.Space8))
        RecommendationGrid(items = state.items, onOpen = onOpen)
        if (state.isLoading) {
            Spacer(Modifier.height(WearTokens.Space8))
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(WearTokens.Space8))
                Text(
                    text = if (state.items.isEmpty()) "正在加载缩略图" else "下一批加载中，当前内容保留",
                    fontSize = 11.sp,
                )
            }
        }
        state.errorMessage?.let { message ->
            Spacer(Modifier.height(WearTokens.Space8))
            Text(
                text = message,
                color = MaterialTheme.colors.error,
                fontSize = 11.sp,
                textAlign = TextAlign.Center,
            )
        }
        Spacer(Modifier.height(WearTokens.Space8))
        Chip(
            onClick = onRefresh,
            enabled = !state.isLoading,
            label = {
                Text(
                    text = if (state.items.isEmpty()) "重试" else "换一批",
                    modifier = Modifier.fillMaxWidth(),
                    textAlign = TextAlign.Center,
                )
            },
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(WearTokens.Space16))
    }
}

@Composable
private fun RecommendationGrid(
    items: List<RecommendationItem>,
    onOpen: (String) -> Unit,
) {
    val slots = List(5) { index -> items.getOrNull(index) }
    slots.chunked(2).forEach { rowItems ->
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(WearTokens.Space8),
        ) {
            rowItems.forEach { item ->
                RecommendationTile(
                    item = item,
                    onOpen = onOpen,
                    modifier = Modifier.weight(1f),
                )
            }
            if (rowItems.size == 1) Spacer(Modifier.weight(1f))
        }
        Spacer(Modifier.height(WearTokens.Space8))
    }
}

@Composable
private fun RecommendationTile(
    item: RecommendationItem?,
    onOpen: (String) -> Unit,
    modifier: Modifier,
) {
    val shape = RoundedCornerShape(WearTokens.Radius12)
    Box(
        modifier = modifier
            .aspectRatio(1.08f)
            .clip(shape)
            .background(MaterialTheme.colors.surface)
            .then(if (item != null) Modifier.clickable { onOpen(item.itemId) } else Modifier),
        contentAlignment = Alignment.Center,
    ) {
        if (item == null) {
            Text(
                text = "…",
                color = MaterialTheme.colors.onSurface.copy(alpha = 0.35f),
            )
        } else {
            AsyncImage(
                model = item.thumbnailUrl,
                contentDescription = item.name.ifBlank { "推荐图片" },
                modifier = Modifier.fillMaxSize(),
                contentScale = ContentScale.Crop,
            )
        }
    }
}

@Composable
private fun OriginalScreen(
    item: RecommendationItem,
) {
    val context = LocalContext.current
    var retry by remember(item.itemId) { mutableIntStateOf(0) }
    var viewport by remember(item.itemId) { mutableStateOf(IntSize.Zero) }
    var transform by remember(item.itemId) { mutableStateOf(ZoomTransform()) }
    val transformableState = rememberTransformableState { zoomChange, panChange, _ ->
        transform = updatedZoomTransform(
            current = transform,
            zoomChange = zoomChange,
            panChange = panChange,
            viewport = viewport,
        )
    }
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black),
    ) {
        Box(
            modifier = Modifier
                .fillMaxSize()
                .onSizeChanged { viewport = it }
                .transformable(transformableState),
        ) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .graphicsLayer {
                        scaleX = transform.scale
                        scaleY = transform.scale
                        translationX = transform.offset.x
                        translationY = transform.offset.y
                    },
            ) {
                AsyncImage(
                    model = item.thumbnailUrl,
                    contentDescription = null,
                    modifier = Modifier.fillMaxSize(),
                    contentScale = ContentScale.Fit,
                )
                key(retry) {
                    SubcomposeAsyncImage(
                        model = ImageRequest.Builder(context)
                            .data(item.previewUrl)
                            .size(ORIGINAL_CACHE_SIZE, ORIGINAL_CACHE_SIZE)
                            .memoryCachePolicy(CachePolicy.ENABLED)
                            .diskCachePolicy(CachePolicy.ENABLED)
                            .build(),
                        contentDescription = item.name.ifBlank { "原图" },
                        modifier = Modifier.fillMaxSize(),
                        contentScale = ContentScale.Fit,
                        loading = {
                            Box(modifier = Modifier.fillMaxSize()) {
                                Column(
                                    modifier = Modifier
                                        .align(Alignment.BottomCenter)
                                        .padding(bottom = 20.dp),
                                    horizontalAlignment = Alignment.CenterHorizontally,
                                ) {
                                    CircularProgressIndicator(modifier = Modifier.size(22.dp))
                                    Spacer(Modifier.height(WearTokens.Space4))
                                    Text(
                                        text = "原图加载中",
                                        color = Color.White,
                                        fontSize = 11.sp,
                                    )
                                }
                            }
                        },
                        success = { SubcomposeAsyncImageContent() },
                        error = {
                            Box(
                                modifier = Modifier.fillMaxSize(),
                                contentAlignment = Alignment.Center,
                            ) {
                                Chip(
                                    onClick = { retry += 1 },
                                    label = {
                                        Text(
                                            text = "原图加载失败，重试",
                                            modifier = Modifier.fillMaxWidth(),
                                            textAlign = TextAlign.Center,
                                        )
                                    },
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .padding(horizontal = 36.dp),
                                )
                            }
                        },
                    )
                }
            }
        }
    }
}

internal data class ZoomTransform(
    val scale: Float = MIN_ORIGINAL_SCALE,
    val offset: Offset = Offset.Zero,
)

internal fun updatedZoomTransform(
    current: ZoomTransform,
    zoomChange: Float,
    panChange: Offset,
    viewport: IntSize,
): ZoomTransform {
    val nextScale = (current.scale * zoomChange)
        .coerceIn(MIN_ORIGINAL_SCALE, MAX_ORIGINAL_SCALE)
    if (nextScale == MIN_ORIGINAL_SCALE) return ZoomTransform()
    val scaleRatio = nextScale / current.scale
    val maxOffsetX = viewport.width * (nextScale - 1f) / 2f
    val maxOffsetY = viewport.height * (nextScale - 1f) / 2f
    return ZoomTransform(
        scale = nextScale,
        offset = Offset(
            x = (current.offset.x * scaleRatio + panChange.x)
                .coerceIn(-maxOffsetX, maxOffsetX),
            y = (current.offset.y * scaleRatio + panChange.y)
                .coerceIn(-maxOffsetY, maxOffsetY),
        ),
    )
}

private const val MIN_ORIGINAL_SCALE = 1f
private const val MAX_ORIGINAL_SCALE = 5f
