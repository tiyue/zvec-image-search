package com.zvec.lanviewer.ui.search

import android.content.Context
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import coil.request.ImageRequest
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.ui.AppUiState
import com.zvec.lanviewer.ui.GALLERY_EDGE_PADDING_DP
import com.zvec.lanviewer.ui.GALLERY_SPACING_DP
import com.zvec.lanviewer.ui.GalleryDensity
import com.zvec.lanviewer.ui.GalleryDensityEntry
import com.zvec.lanviewer.ui.SearchPhase
import com.zvec.lanviewer.ui.canStartNextPage
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.filterNotNull

internal const val SEARCH_RESULTS_GRID_TAG = "search-results-grid"
internal const val NEXT_PAGE_RETRY_TAG = "search-next-page-retry"
internal const val GALLERY_CARD_TAG = "gallery-card"

private const val THUMBNAIL_DECODE_SIZE = 640
private const val LOAD_MORE_THRESHOLD = 8

private enum class ThumbnailLoadState { LOADING, SUCCESS, ERROR }

/**
 * Thumbnail request shared by the grids and the viewer placeholder. Both call sites must keep using
 * this builder so Coil's stable (data derived) memory cache key matches and the viewer can reuse the
 * thumbnail the grid already loaded instead of downloading it again.
 *
 * The first attempt carries no extra parameter, so it stays byte-for-byte compatible with the
 * recommendation preload request; only an explicit retry adds the parameter that forces Coil to run
 * the request again.
 */
internal fun thumbnailImageRequest(context: Context, url: String, attempt: Int = 0): ImageRequest {
    val builder = ImageRequest.Builder(context)
        .data(url)
        .memoryCacheKey(url)
        .size(THUMBNAIL_DECODE_SIZE)
        .crossfade(true)
    if (attempt > 0) {
        builder.setParameter("thumbnailRetry", attempt)
    }
    return builder.build()
}

/**
 * One gallery thumbnail. Uses plain AsyncImage with same-size loading/error overlays instead of a
 * subcomposing image, so scrolling the lazy grid stays cheap. The image is Fit (never cropped) and
 * the box height is fixed per density, so loading, failure and success all keep the same height.
 */
@Composable
internal fun GalleryThumbnailCard(
    name: String,
    url: String,
    height: Dp,
    onClick: () -> Unit,
) {
    val context = LocalContext.current
    var attempt by remember(url) { mutableIntStateOf(0) }
    var loadState by remember(url) { mutableStateOf(ThumbnailLoadState.LOADING) }
    Card(
        modifier = Modifier.fillMaxWidth().testTag(GALLERY_CARD_TAG).clickable(onClick = onClick),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(height)
                .background(MaterialTheme.colorScheme.surfaceVariant),
            contentAlignment = Alignment.Center,
        ) {
            AsyncImage(
                model = thumbnailImageRequest(context, url, attempt),
                contentDescription = name,
                contentScale = ContentScale.Fit,
                modifier = Modifier.fillMaxSize(),
                onLoading = { loadState = ThumbnailLoadState.LOADING },
                onSuccess = { loadState = ThumbnailLoadState.SUCCESS },
                onError = { loadState = ThumbnailLoadState.ERROR },
            )
            when (loadState) {
                ThumbnailLoadState.LOADING -> CircularProgressIndicator(Modifier.size(24.dp))
                ThumbnailLoadState.ERROR -> Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text("加载失败", style = MaterialTheme.typography.bodySmall)
                    TextButton(
                        onClick = {
                            attempt += 1
                            loadState = ThumbnailLoadState.LOADING
                        },
                    ) { Text("重试") }
                }
                ThumbnailLoadState.SUCCESS -> Unit
            }
        }
        Text(
            name.ifBlank { "未命名图片" },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 6.dp),
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/**
 * Short query description for the compact results header. Once a search has been submitted the
 * summary comes from that snapshot, so editing the next draft never rewrites the finished title;
 * only a still-unsubmitted state falls back to the live draft.
 */
internal fun searchSummary(state: AppUiState): String {
    val submitted = state.submittedSearchText
    if (submitted != null) {
        return when {
            submitted.isNotEmpty() -> submitted
            state.submittedSearchHasImage -> "以图搜索"
            else -> "全部结果"
        }
    }
    val draft = state.searchText.trim()
    return when {
        draft.isNotEmpty() -> draft
        state.queryImage != null -> "以图搜索"
        else -> "全部结果"
    }
}

internal fun loadedCountLabel(state: AppUiState): String =
    if (state.totalResults > 0) {
        "已加载 ${state.results.size} / ${state.totalResults}"
    } else {
        "已加载 ${state.results.size}"
    }

/**
 * The RESULTS tab body. Every search phase is rendered explicitly so an empty draft, a successful
 * zero-result search, a first-page failure and a cancelled search never look the same.
 */
@Composable
internal fun SearchResultsPane(
    state: AppUiState,
    gridState: LazyGridState,
    thumbnailUrl: (SearchItem) -> String,
    density: GalleryDensity,
    onDensityChange: (GalleryDensity) -> Unit,
    onOpen: (Int) -> Unit,
    onLoadMore: () -> Unit,
    onRetryPage: () -> Unit,
    onRetrySearch: () -> Unit,
    onModifySearch: () -> Unit,
    modifier: Modifier,
) {
    Column(modifier.fillMaxSize()) {
        if (state.results.isNotEmpty()) {
            ResultsHeader(
                state = state,
                density = density,
                onDensityChange = onDensityChange,
                onModifySearch = onModifySearch,
            )
        }
        Box(Modifier.weight(1f).fillMaxWidth()) {
            when {
                state.results.isNotEmpty() -> ResultsGrid(
                    state = state,
                    gridState = gridState,
                    thumbnailUrl = thumbnailUrl,
                    density = density,
                    onOpen = onOpen,
                    onLoadMore = onLoadMore,
                    onRetryPage = onRetryPage,
                    modifier = Modifier.fillMaxSize(),
                )
                state.searchPhase == SearchPhase.SEARCHING || state.isSearching -> SearchLoadingState(
                    status = state.searchStatus,
                    modifier = Modifier.fillMaxSize(),
                )
                state.searchPhase == SearchPhase.ERROR -> SearchMessageState(
                    title = "搜索失败",
                    detail = state.searchError,
                    primary = SearchResultAction("重试", onRetrySearch),
                    secondary = SearchResultAction("修改搜索", onModifySearch),
                    modifier = Modifier.fillMaxSize(),
                )
                state.searchPhase == SearchPhase.CANCELLED -> SearchMessageState(
                    title = "已取消",
                    detail = null,
                    primary = SearchResultAction("修改搜索", onModifySearch),
                    secondary = null,
                    modifier = Modifier.fillMaxSize(),
                )
                state.searchPhase == SearchPhase.LOADED -> SearchMessageState(
                    title = "没有找到匹配图片",
                    detail = null,
                    primary = SearchResultAction("修改搜索", onModifySearch),
                    secondary = null,
                    modifier = Modifier.fillMaxSize(),
                )
                else -> SearchMessageState(
                    title = "搜索后在这里查看结果",
                    detail = null,
                    primary = null,
                    secondary = null,
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
    }
}

/**
 * Compact header row: query summary, loaded/total, the density entry and an icon button that returns
 * to the search page. The complex choices stay in the small density menu.
 */
@Composable
private fun ResultsHeader(
    state: AppUiState,
    density: GalleryDensity,
    onDensityChange: (GalleryDensity) -> Unit,
    onModifySearch: () -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(start = 12.dp, end = 4.dp, top = 4.dp, bottom = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                searchSummary(state),
                style = MaterialTheme.typography.titleSmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                loadedCountLabel(state),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        GalleryDensityEntry(density = density, onDensityChange = onDensityChange)
        IconButton(onClick = onModifySearch, modifier = Modifier.size(48.dp)) {
            Icon(Icons.Default.Edit, contentDescription = "修改搜索")
        }
    }
}

private data class SearchResultAction(val label: String, val onClick: () -> Unit)

@Composable
private fun SearchLoadingState(status: String?, modifier: Modifier) {
    Column(
        modifier = modifier.padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        CircularProgressIndicator()
        if (status != null) {
            Spacer(Modifier.height(12.dp))
            Text(status, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.secondary)
        }
    }
}

@Composable
private fun SearchMessageState(
    title: String,
    detail: String?,
    primary: SearchResultAction?,
    secondary: SearchResultAction?,
    modifier: Modifier,
) {
    Column(
        modifier = modifier.padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(title, style = MaterialTheme.typography.titleMedium, textAlign = TextAlign.Center)
        if (detail != null) {
            Spacer(Modifier.height(6.dp))
            Text(
                detail,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.secondary,
                textAlign = TextAlign.Center,
            )
        }
        if (primary != null || secondary != null) {
            Spacer(Modifier.height(12.dp))
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                primary?.let { TextButton(onClick = it.onClick) { Text(it.label) } }
                secondary?.let { TextButton(onClick = it.onClick) { Text(it.label) } }
            }
        }
    }
}

@Composable
private fun ResultsGrid(
    state: AppUiState,
    gridState: LazyGridState,
    thumbnailUrl: (SearchItem) -> String,
    density: GalleryDensity,
    onOpen: (Int) -> Unit,
    onLoadMore: () -> Unit,
    onRetryPage: () -> Unit,
    modifier: Modifier,
) {
    // Read the newest state and callback so a stale size or a failed page can never trigger an
    // endless auto retry: the page gate blocks while loading or while an error is shown.
    val latestState by rememberUpdatedState(state)
    val latestLoadMore by rememberUpdatedState(onLoadMore)
    LaunchedEffect(gridState) {
        snapshotFlow { gridState.layoutInfo.visibleItemsInfo.lastOrNull()?.index }
            .filterNotNull()
            .distinctUntilChanged()
            .collect { lastVisible ->
                val current = latestState
                val lastIndex = current.results.lastIndex
                if (lastIndex >= 0 && lastVisible >= lastIndex - LOAD_MORE_THRESHOLD && current.canStartNextPage()) {
                    latestLoadMore()
                }
            }
    }
    LazyVerticalGrid(
        columns = GridCells.Adaptive(density.targetCellWidthDp.dp),
        state = gridState,
        modifier = modifier.testTag(SEARCH_RESULTS_GRID_TAG),
        contentPadding = PaddingValues(GALLERY_EDGE_PADDING_DP.dp),
        horizontalArrangement = Arrangement.spacedBy(GALLERY_SPACING_DP.dp),
        verticalArrangement = Arrangement.spacedBy(GALLERY_SPACING_DP.dp),
    ) {
        itemsIndexed(
            state.results,
            key = { index, item -> "${item.mediaId}:$index" },
        ) { index, item ->
            GalleryThumbnailCard(
                name = item.name,
                url = thumbnailUrl(item),
                height = density.cellHeightDp.dp,
                onClick = { onOpen(index) },
            )
        }
        if (state.isLoadingNextPage) {
            item(span = { GridItemSpan(maxLineSpan) }) {
                Box(Modifier.fillMaxWidth().padding(20.dp), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(Modifier.size(28.dp))
                }
            }
        }
        val nextPageError = state.nextPageError
        if (nextPageError != null) {
            item(span = { GridItemSpan(maxLineSpan) }) {
                NextPageError(message = nextPageError, onRetry = onRetryPage)
            }
        }
    }
}

@Composable
private fun NextPageError(message: String, onRetry: () -> Unit) {
    Column(
        Modifier.fillMaxWidth().padding(12.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Text("下一页加载失败", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.error)
        Text(message, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.secondary)
        TextButton(onClick = onRetry, modifier = Modifier.testTag(NEXT_PAGE_RETRY_TAG)) { Text("重试") }
    }
}
