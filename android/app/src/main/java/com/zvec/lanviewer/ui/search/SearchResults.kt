package com.zvec.lanviewer.ui.search

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
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
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
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImagePainter
import coil.compose.SubcomposeAsyncImage
import coil.compose.SubcomposeAsyncImageContent
import coil.request.ImageRequest
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.ui.AppUiState
import com.zvec.lanviewer.ui.SearchPhase
import com.zvec.lanviewer.ui.canStartNextPage
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.filterNotNull
import java.text.DecimalFormat

internal const val SEARCH_RESULTS_GRID_TAG = "search-results-grid"
internal const val NEXT_PAGE_RETRY_TAG = "search-next-page-retry"

private const val THUMBNAIL_DECODE_SIZE = 640
private const val LOAD_MORE_THRESHOLD = 8

private data class SearchResultAction(val label: String, val onClick: () -> Unit)

/**
 * The RESULTS tab body. Every search phase is rendered explicitly so an empty draft, a successful
 * zero-result search, a first-page failure and a cancelled search never look the same.
 */
@Composable
internal fun SearchResultsPane(
    state: AppUiState,
    gridState: LazyGridState,
    thumbnailUrl: (SearchItem) -> String,
    onOpen: (Int) -> Unit,
    onLoadMore: () -> Unit,
    onRetryPage: () -> Unit,
    onRetrySearch: () -> Unit,
    onModifySearch: () -> Unit,
    modifier: Modifier,
) {
    when {
        state.results.isNotEmpty() -> ResultsGrid(
            state = state,
            gridState = gridState,
            thumbnailUrl = thumbnailUrl,
            onOpen = onOpen,
            onLoadMore = onLoadMore,
            onRetryPage = onRetryPage,
            modifier = modifier,
        )
        state.searchPhase == SearchPhase.SEARCHING || state.isSearching -> SearchLoadingState(
            status = state.searchStatus,
            modifier = modifier,
        )
        state.searchPhase == SearchPhase.ERROR -> SearchMessageState(
            title = "搜索失败",
            detail = state.searchError,
            primary = SearchResultAction("重试", onRetrySearch),
            secondary = SearchResultAction("修改搜索", onModifySearch),
            modifier = modifier,
        )
        state.searchPhase == SearchPhase.CANCELLED -> SearchMessageState(
            title = "已取消",
            detail = null,
            primary = SearchResultAction("修改搜索", onModifySearch),
            secondary = null,
            modifier = modifier,
        )
        state.searchPhase == SearchPhase.LOADED -> SearchMessageState(
            title = "没有找到匹配图片",
            detail = null,
            primary = SearchResultAction("修改搜索", onModifySearch),
            secondary = null,
            modifier = modifier,
        )
        else -> SearchMessageState(
            title = "搜索后在这里查看结果",
            detail = null,
            primary = null,
            secondary = null,
            modifier = modifier,
        )
    }
}

@Composable
private fun SearchLoadingState(status: String?, modifier: Modifier) {
    Column(
        modifier = modifier.fillMaxSize().padding(24.dp),
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
        modifier = modifier.fillMaxSize().padding(24.dp),
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
        columns = GridCells.Fixed(2),
        state = gridState,
        modifier = modifier.testTag(SEARCH_RESULTS_GRID_TAG),
        contentPadding = PaddingValues(8.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        itemsIndexed(
            state.results,
            key = { index, item -> "${item.mediaId}:$index" },
        ) { index, item ->
            ResultCard(item = item, url = thumbnailUrl(item), onClick = { onOpen(index) })
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

@Composable
private fun ResultCard(item: SearchItem, url: String, onClick: () -> Unit) {
    val context = LocalContext.current
    var attempt by remember(url) { mutableIntStateOf(0) }
    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
    ) {
        SubcomposeAsyncImage(
            model = ImageRequest.Builder(context)
                .data(url)
                .size(THUMBNAIL_DECODE_SIZE)
                .crossfade(true)
                .setParameter("thumbnailRetry", attempt)
                .build(),
            contentDescription = item.name,
            contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxWidth().height(190.dp).background(MaterialTheme.colorScheme.surfaceVariant),
        ) {
            when (painter.state) {
                is AsyncImagePainter.State.Error -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("加载失败", style = MaterialTheme.typography.bodySmall)
                        TextButton(onClick = { attempt += 1 }) { Text("重试") }
                    }
                }
                is AsyncImagePainter.State.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(Modifier.size(28.dp))
                }
                else -> SubcomposeAsyncImageContent()
            }
        }
        Column(Modifier.padding(8.dp)) {
            Text(item.name.ifBlank { "未命名图片" }, maxLines = 1, overflow = TextOverflow.Ellipsis)
            val detail = buildList {
                item.score?.let { add(DecimalFormat("0.000").format(it)) }
                item.libraryName?.let(::add)
            }.joinToString(" · ")
            if (detail.isNotBlank()) {
                Text(detail, style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
    }
}
