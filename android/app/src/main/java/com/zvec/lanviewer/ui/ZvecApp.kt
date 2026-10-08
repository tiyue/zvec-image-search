@file:OptIn(
    androidx.compose.foundation.ExperimentalFoundationApi::class,
    androidx.compose.material3.ExperimentalMaterial3Api::class,
)

package com.zvec.lanviewer.ui

import android.content.ClipData
import android.content.Intent
import android.net.Uri
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.calculatePan
import androidx.compose.foundation.gestures.calculateZoom
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.saveable.Saver
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import coil.compose.SubcomposeAsyncImage
import coil.compose.SubcomposeAsyncImageContent
import coil.request.ImageRequest
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.zvec.lanviewer.data.local.SavedFileRecord
import com.zvec.lanviewer.data.local.ServerAddress
import com.zvec.lanviewer.data.model.DiscoveredServer
import com.zvec.lanviewer.data.model.LibraryDto
import com.zvec.lanviewer.data.model.OriginalMediaItem
import com.zvec.lanviewer.data.model.RecommendationAction
import com.zvec.lanviewer.data.model.RecommendationItem
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.SearchMode
import com.zvec.lanviewer.ui.search.QueryImageControl
import com.zvec.lanviewer.ui.search.SearchResultsPane
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import java.text.DecimalFormat
import java.text.SimpleDateFormat

@Composable
fun ZvecApp(viewModel: AppViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val snackbarHostState = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    var selectedTab by rememberSaveable { mutableStateOf(MobileTab.SEARCH) }
    var awaitingFirstResult by rememberSaveable { mutableStateOf(false) }
    var handledSearchCompletion by rememberSaveable { mutableStateOf(state.searchCompletionSignal) }
    val searchGridState = rememberLazyGridState()
    val recommendationGridState = rememberLazyGridState()

    // Search and recommendation each keep their own lifted, saveable grid state, so opening the
    // viewer or switching tabs never loses the browse position.
    SearchGridAnchorEffect(gridState = searchGridState, searchAttempt = state.searchAttempt)

    LaunchedEffect(state.searchCompletionSignal) {
        val decision = searchNavigationAfterCompletion(
            awaitingFirstResult = awaitingFirstResult,
            currentTab = selectedTab,
            completionSignal = state.searchCompletionSignal,
            handledCompletionSignal = handledSearchCompletion,
        )
        handledSearchCompletion = state.searchCompletionSignal
        selectedTab = decision.targetTab
        awaitingFirstResult = decision.awaitingFirstResult
    }
    LaunchedEffect(state.searchPhase) {
        if (state.searchPhase == SearchPhase.ERROR || state.searchPhase == SearchPhase.CANCELLED) {
            awaitingFirstResult = false
        }
    }
    LaunchedEffect(state.phase) {
        selectedTab = mobileTabAfterPhaseChange(selectedTab, state.phase)
    }

    val imagePicker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        uri?.let {
            runCatching {
                context.contentResolver.takePersistableUriPermission(
                    it,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION,
                )
            }
            viewModel.selectQueryImage(it)
        }
    }

    // The save destination picker captures the item at launch and stores only a small snapshot, so
    // the callback never rebinds to a different image after the viewer or search moved on.
    var pendingSaveTarget by rememberSaveable(stateSaver = PendingSaveTargetSaver) {
        mutableStateOf<PendingSaveTarget?>(null)
    }
    val viewerItem = state.currentViewerItem()
    val saveLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument(viewerItem?.contentType ?: "image/*"),
    ) { destination: Uri? ->
        val target = pendingSaveTarget
        pendingSaveTarget = null
        if (destination != null) {
            val pinnedItem = target?.resolve(viewModel.state.value)
            if (pinnedItem != null) {
                viewModel.saveItem(pinnedItem, destination)
            } else {
                scope.launch {
                    snackbarHostState.showSnackbar("保存目标已失效，请重新打开图片后再保存")
                }
            }
        }
    }

    LaunchedEffect(viewModel) {
        viewModel.events.collect { event ->
            when (event) {
                is AppEvent.Message -> snackbarHostState.showSnackbar(event.text)
                is AppEvent.Share -> {
                    val launched = runCatching {
                        val intent = Intent(Intent.ACTION_SEND).apply {
                            type = event.mimeType
                            putExtra(Intent.EXTRA_STREAM, event.uri)
                            clipData = ClipData.newUri(context.contentResolver, "YaoLens original", event.uri)
                            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                        }
                        context.startActivity(Intent.createChooser(intent, "分享原图"))
                    }
                    if (launched.isFailure) {
                        val reason = launched.exceptionOrNull()?.message ?: "未知原因"
                        snackbarHostState.showSnackbar("无法启动分享：$reason")
                    }
                }
            }
        }
    }

    val viewerOpen = state.viewerIndex != null && state.viewerItems().isNotEmpty()

    // The viewer and the normal pages share one visible SnackbarHost, so save and share messages
    // are shown on the current screen instead of being sent to a host that the early return skipped.
    Box(Modifier.fillMaxSize()) {
        if (viewerOpen) {
            OriginalViewer(
                state = state,
                mediaUrl = viewModel::mediaUrl,
                onIndexChanged = viewModel::setViewerIndex,
                onClose = viewModel::closeViewer,
                onSave = {
                    viewerItem?.let { item ->
                        pendingSaveTarget = PendingSaveTarget.of(item, state.viewerSource)
                        saveLauncher.launch(item.name.ifBlank { "yaolens-original" })
                    }
                },
                onShare = viewModel::shareCurrent,
                onCancelTransfer = viewModel::cancelTransfer,
                onReaction = viewModel::reactToRecommendation,
            )
        } else {
            Scaffold(
                modifier = Modifier.fillMaxSize(),
                containerColor = MaterialTheme.colorScheme.background,
            ) { padding ->
                when (state.phase) {
                    ConnectionPhase.DISCOVERY -> DiscoveryScreen(
                        state = state,
                        modifier = Modifier.padding(padding),
                        onDiscover = viewModel::discover,
                        onServer = viewModel::connect,
                        onManual = viewModel::connectManual,
                    )
                    ConnectionPhase.CONNECTING -> CenterStatus(
                        title = "正在连接",
                        detail = state.connectionDetail ?: state.serverName ?: "YaoLens 电脑",
                        modifier = Modifier.padding(padding),
                    )
                    ConnectionPhase.PAIRING -> PairingScreen(
                        state = state,
                        modifier = Modifier.padding(padding),
                        onCancel = viewModel::cancelPairing,
                    )
                    ConnectionPhase.READY -> SearchScreen(
                        state = state,
                        selectedTab = selectedTab,
                        searchGridState = searchGridState,
                        recommendationGridState = recommendationGridState,
                        modifier = Modifier.padding(padding),
                        thumbnailUrl = { item -> viewModel.thumbnailUrl(item) },
                        transferMessage = state.transferMessage,
                        transferFraction = state.transferFraction,
                        onCancelTransfer = viewModel::cancelTransfer,
                        onSelectedTab = { tab ->
                            awaitingFirstResult = searchNavigationAfterTabSelected(awaitingFirstResult, tab)
                            selectedTab = tab
                        },
                        onMode = viewModel::setSearchMode,
                        onText = viewModel::setSearchText,
                        onTopK = viewModel::setTopK,
                        onLibrary = viewModel::toggleLibrary,
                        onPickImage = { imagePicker.launch("image/*") },
                        onCancelUpload = viewModel::cancelQueryUpload,
                        onRetryUpload = viewModel::retryQueryUpload,
                        onClearImage = viewModel::clearQueryImage,
                        onSearch = {
                            val previousAttempt = viewModel.state.value.searchAttempt
                            viewModel.search()
                            awaitingFirstResult = viewModel.state.value.searchAttempt != previousAttempt
                        },
                        onCancelSearch = {
                            awaitingFirstResult = false
                            viewModel.cancelSearch()
                        },
                        onRetrySearch = viewModel::retrySearch,
                        onLoadMore = viewModel::loadNextPage,
                        onRetryPage = viewModel::retryNextPage,
                        onOpen = viewModel::openViewer,
                        onLoadRecommendations = viewModel::loadRecommendations,
                        onRecommendationsVisible = viewModel::onRecommendationsVisibilityChanged,
                        onOpenRecommendation = viewModel::openRecommendation,
                        onDisconnect = viewModel::disconnect,
                    )
                }
            }
        }
        SnackbarHost(
            hostState = snackbarHostState,
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .testTag(APP_SNACKBAR_HOST_TAG),
        )
    }
}

@Composable
private fun DiscoveryScreen(
    state: AppUiState,
    modifier: Modifier,
    onDiscover: () -> Unit,
    onServer: (DiscoveredServer) -> Unit,
    onManual: (String, String) -> Unit,
) {
    var host by remember { mutableStateOf("") }
    var port by remember { mutableStateOf(ServerAddress.DEFAULT_PORT.toString()) }

    LazyColumn(
        modifier = modifier.fillMaxSize().statusBarsPadding(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item {
            Text("YaoLens", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(6.dp))
            Text("在同一局域网内发现 Windows 上的 YaoLens。首次连接需要在电脑端确认六位码。")
        }
        item {
            Surface(
                color = MaterialTheme.colorScheme.errorContainer,
                shape = RoundedCornerShape(12.dp),
            ) {
                Text(
                    "当前 Preview 使用未加密 HTTP，仅可连接你自己控制的家庭专用网络。不要在公共、共享或访客 Wi-Fi 使用。",
                    modifier = Modifier.fillMaxWidth().padding(12.dp),
                    color = MaterialTheme.colorScheme.onErrorContainer,
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        }
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("自动发现", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.weight(1f))
                OutlinedButton(onClick = onDiscover, enabled = !state.isDiscovering) {
                    Text(if (state.isDiscovering) "搜索中…" else "重新搜索")
                }
            }
        }
        if (state.isDiscovering) item { LinearProgressIndicator(Modifier.fillMaxWidth()) }
        items(state.discoveredServers.size, key = { state.discoveredServers[it].stableKey }) { index ->
            val server = state.discoveredServers[index]
            Card(
                modifier = Modifier.fillMaxWidth().clickable { onServer(server) },
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            ) {
                Column(Modifier.padding(16.dp)) {
                    Text(server.name, fontWeight = FontWeight.SemiBold)
                    Text("${server.host}:${server.port}", style = MaterialTheme.typography.bodySmall)
                }
            }
        }
        item {
            Text("手工连接", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = host,
                    onValueChange = { host = it },
                    label = { Text("电脑 IP") },
                    placeholder = { Text("192.168.1.20") },
                    singleLine = true,
                    modifier = Modifier.weight(1f),
                )
                OutlinedTextField(
                    value = port,
                    onValueChange = { if (it.all(Char::isDigit)) port = it },
                    label = { Text("端口") },
                    singleLine = true,
                    modifier = Modifier.width(112.dp),
                )
            }
            Spacer(Modifier.height(10.dp))
            Button(onClick = { onManual(host, port) }, modifier = Modifier.fillMaxWidth()) {
                Text("连接")
            }
        }
        state.errorMessage?.let { message -> item { ErrorBanner(message) } }
    }
}

@Composable
private fun PairingScreen(state: AppUiState, modifier: Modifier, onCancel: () -> Unit) {
    Column(
        modifier = modifier.fillMaxSize().statusBarsPadding().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text("确认配对码", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(12.dp))
        Text("请确认电脑端显示相同的六位码，然后在电脑上允许连接。")
        Spacer(Modifier.height(28.dp))
        Surface(
            color = MaterialTheme.colorScheme.primaryContainer,
            shape = RoundedCornerShape(18.dp),
        ) {
            Text(
                text = state.pairingCode ?: "——— ———",
                modifier = Modifier.padding(horizontal = 28.dp, vertical = 20.dp),
                fontSize = 40.sp,
                fontWeight = FontWeight.Bold,
                letterSpacing = 4.sp,
                color = MaterialTheme.colorScheme.onPrimaryContainer,
            )
        }
        Spacer(Modifier.height(18.dp))
        Text("剩余 ${state.pairingSecondsRemaining} 秒")
        Spacer(Modifier.height(20.dp))
        CircularProgressIndicator()
        Spacer(Modifier.height(24.dp))
        OutlinedButton(onClick = onCancel) { Text("取消") }
    }
}

@Composable
private fun CenterStatus(title: String, detail: String, modifier: Modifier) {
    Column(
        modifier = modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        CircularProgressIndicator()
        Spacer(Modifier.height(18.dp))
        Text(title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
        Text(detail, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
internal fun SearchScreen(
    state: AppUiState,
    selectedTab: MobileTab,
    searchGridState: LazyGridState,
    recommendationGridState: LazyGridState,
    modifier: Modifier,
    thumbnailUrl: (SearchItem) -> String,
    transferMessage: String?,
    transferFraction: Float?,
    onCancelTransfer: () -> Unit,
    onSelectedTab: (MobileTab) -> Unit,
    onMode: (SearchMode) -> Unit,
    onText: (String) -> Unit,
    onTopK: (String) -> Unit,
    onLibrary: (String) -> Unit,
    onPickImage: () -> Unit,
    onCancelUpload: () -> Unit,
    onRetryUpload: () -> Unit,
    onClearImage: () -> Unit,
    onSearch: () -> Unit,
    onCancelSearch: () -> Unit,
    onRetrySearch: () -> Unit,
    onLoadMore: () -> Unit,
    onRetryPage: () -> Unit,
    onOpen: (Int) -> Unit,
    onLoadRecommendations: () -> Unit,
    onRecommendationsVisible: (Boolean) -> Unit,
    onOpenRecommendation: (Int) -> Unit,
    onDisconnect: () -> Unit,
) {
    Column(modifier.fillMaxSize().statusBarsPadding()) {
        Box(Modifier.weight(1f).fillMaxWidth()) {
            when (selectedTab) {
                MobileTab.SEARCH -> SearchControls(
                    state = state,
                    onMode = onMode,
                    onText = onText,
                    onTopK = onTopK,
                    onLibrary = onLibrary,
                    onPickImage = onPickImage,
                    onCancelUpload = onCancelUpload,
                    onRetryUpload = onRetryUpload,
                    onClearImage = onClearImage,
                    onSearch = onSearch,
                    onCancelSearch = onCancelSearch,
                )
                MobileTab.RESULTS -> SearchResultsPane(
                    state = state,
                    gridState = searchGridState,
                    thumbnailUrl = thumbnailUrl,
                    onOpen = onOpen,
                    onLoadMore = onLoadMore,
                    onRetryPage = onRetryPage,
                    onRetrySearch = onRetrySearch,
                    onModifySearch = { onSelectedTab(MobileTab.SEARCH) },
                    modifier = Modifier.fillMaxSize(),
                )
                MobileTab.RECOMMENDATIONS -> RecommendationPanel(
                    state = state,
                    onLoad = onLoadRecommendations,
                    onVisible = onRecommendationsVisible,
                    onOpen = onOpenRecommendation,
                    gridState = recommendationGridState,
                )
                MobileTab.DEVICE -> DevicePanel(state = state, onDisconnect = onDisconnect)
            }
        }

        if (transferMessage != null) {
            TransferStatusBar(
                message = transferMessage,
                fraction = transferFraction,
                onCancel = onCancelTransfer,
            )
        }
        MobileBottomBar(selected = selectedTab, onSelected = onSelectedTab)
    }
}

@Composable
private fun MobileBottomBar(selected: MobileTab, onSelected: (MobileTab) -> Unit) {
    NavigationBar(modifier = Modifier.fillMaxWidth().navigationBarsPadding()) {
        listOf(
            Triple(MobileTab.SEARCH, "⌕", "搜索"),
            Triple(MobileTab.RESULTS, "▧", "结果"),
            Triple(MobileTab.RECOMMENDATIONS, "✦", "推荐"),
            Triple(MobileTab.DEVICE, "▣", "设备"),
        ).forEach { (tab, glyph, label) ->
            NavigationBarItem(
                selected = selected == tab,
                onClick = { onSelected(tab) },
                icon = { Text(glyph, fontSize = 18.sp) },
                label = { Text(label) },
                alwaysShowLabel = true,
            )
        }
    }
}

@Composable
internal fun RecommendationPanel(
    state: AppUiState,
    onLoad: () -> Unit,
    onVisible: (Boolean) -> Unit,
    onOpen: (Int) -> Unit,
    gridState: LazyGridState,
) {
    val recommendations = state.recommendations
    val lifecycleOwner = LocalLifecycleOwner.current
    var isResumed by remember(lifecycleOwner) {
        mutableStateOf(lifecycleOwner.lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED))
    }
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, _ ->
            isResumed = lifecycleOwner.lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            onVisible(false)
        }
    }
    LaunchedEffect(lifecycleOwner, isResumed) {
        onVisible(isResumed)
    }
    Column(Modifier.fillMaxSize()) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text("推荐", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
                if (recommendations.partial) {
                    Text(
                        recommendations.partialReason ?: "可用图片不足，已展示当前结果",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.secondary,
                    )
                }
            }
            TextButton(onClick = onLoad, enabled = !recommendations.isLoading) {
                Text(if (recommendations.isLoading) "加载中" else "换一批")
            }
        }
        recommendations.errorMessage?.let { ErrorBanner(it) }
        when {
            recommendations.items.isNotEmpty() -> LazyVerticalGrid(
                columns = GridCells.Fixed(2),
                state = gridState,
                modifier = Modifier.fillMaxSize().testTag(RECOMMENDATION_GRID_TAG),
                contentPadding = PaddingValues(8.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                itemsIndexed(recommendations.items, key = { _, item -> item.itemId }) { index, item ->
                    RecommendationCard(
                        item = item,
                        url = item.thumbnailUrl,
                        onOpen = { onOpen(index) },
                    )
                }
            }
            recommendations.isLoading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
            else -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text("暂时没有可推荐的图片", color = MaterialTheme.colorScheme.secondary)
            }
        }
    }
}

@Composable
internal fun RecommendationCard(
    item: RecommendationItem,
    url: String,
    onOpen: () -> Unit,
) {
    val context = LocalContext.current
    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onOpen),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
    ) {
        AsyncImage(
            model = ImageRequest.Builder(context).data(url).size(640).crossfade(true).build(),
            contentDescription = item.name,
            contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxWidth().height(190.dp).background(MaterialTheme.colorScheme.surfaceVariant),
        )
        Column(Modifier.padding(8.dp)) {
            Text(item.name.ifBlank { "未命名图片" }, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(
                item.libraryName.ifBlank { item.bucket },
                style = MaterialTheme.typography.bodySmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                color = MaterialTheme.colorScheme.secondary,
            )
        }
    }
}

@Composable
private fun DevicePanel(state: AppUiState, onDisconnect: () -> Unit) {
    val context = LocalContext.current
    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Text("连接设备", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.SemiBold)
        }
        item {
            Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
                Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(state.serverName ?: "YaoLens 电脑", fontWeight = FontWeight.SemiBold)
                    Text("已连接 · ${state.libraries.size} 个图库", style = MaterialTheme.typography.bodySmall)
                }
            }
        }
        item {
            Text("已保存文件", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
        }
        if (state.savedFiles.isEmpty()) {
            item {
                Text(
                    "暂无已保存的文件",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.secondary,
                )
            }
        } else {
            items(state.savedFiles.size, key = { "saved-${state.savedFiles[it].uri}-${state.savedFiles[it].savedAtMillis}" }) { index ->
                val record = state.savedFiles[index]
                Card(
                    modifier = Modifier.fillMaxWidth().clickable {
                        runCatching {
                            context.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(Uri.parse(record.uri), "image/*"))
                        }
                    },
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                ) {
                    Column(Modifier.fillMaxWidth().padding(12.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                        Text(record.name, maxLines = 1, overflow = TextOverflow.Ellipsis, fontWeight = FontWeight.Medium)
                        Text(formatTime(record.savedAtMillis), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.secondary)
                    }
                }
            }
        }
        item {
            OutlinedButton(onClick = onDisconnect, modifier = Modifier.fillMaxWidth()) { Text("断开并重新发现") }
        }
    }
}

@Composable
private fun SearchControls(
    state: AppUiState,
    onMode: (SearchMode) -> Unit,
    onText: (String) -> Unit,
    onTopK: (String) -> Unit,
    onLibrary: (String) -> Unit,
    onPickImage: () -> Unit,
    onCancelUpload: () -> Unit,
    onRetryUpload: () -> Unit,
    onClearImage: () -> Unit,
    onSearch: () -> Unit,
    onCancelSearch: () -> Unit,
) {
    var optionsOpen by remember { mutableStateOf(false) }
    val canSearch = when (state.searchMode) {
        SearchMode.TAG -> state.searchText.isNotBlank()
        else -> if (state.queryImage == null) state.searchText.isNotBlank()
            else !state.queryImage.uploading && state.queryImage.queryImageId != null
    }

    if (optionsOpen) {
        ModalBottomSheet(onDismissRequest = { optionsOpen = false }) {
            Column(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp).navigationBarsPadding(),
                verticalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                Text("搜索设置", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
                OutlinedTextField(
                    value = state.topKText,
                    onValueChange = onTopK,
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("结果数量") },
                    singleLine = true,
                )
                if (state.libraries.isNotEmpty()) {
                    Text("搜索图库（不选表示全部）", style = MaterialTheme.typography.labelLarge)
                    LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        items(state.libraries.size, key = { state.libraries[it].id }) { index ->
                            val library = state.libraries[index]
                            LibraryChip(library, library.id in state.selectedLibraryIds) { onLibrary(library.id) }
                        }
                    }
                }
                Button(onClick = { optionsOpen = false }, modifier = Modifier.fillMaxWidth()) { Text("完成") }
                Spacer(Modifier.height(12.dp))
            }
        }
    }

    Column(
        modifier = Modifier.fillMaxSize().padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("你想找什么？", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Medium)
        Spacer(Modifier.height(6.dp))
        Text("搜索电脑上的本地图库", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.secondary)
        Spacer(Modifier.height(22.dp))

        Surface(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(28.dp),
            color = MaterialTheme.colorScheme.surface,
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
            shadowElevation = 2.dp,
        ) {
            Row(
                modifier = Modifier.fillMaxWidth().height(56.dp).padding(horizontal = 7.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                if (state.searchMode != SearchMode.TAG) {
                    TextButton(onClick = onPickImage, modifier = Modifier.size(42.dp), contentPadding = PaddingValues(0.dp)) {
                        Text("＋", fontSize = 22.sp, color = MaterialTheme.colorScheme.secondary)
                    }
                }
                BasicTextField(
                    value = state.searchText,
                    onValueChange = onText,
                    modifier = Modifier.weight(1f).padding(horizontal = 6.dp),
                    singleLine = true,
                    textStyle = MaterialTheme.typography.bodyLarge.copy(color = MaterialTheme.colorScheme.onSurface),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                    keyboardActions = KeyboardActions(onSearch = { if (canSearch) onSearch() }),
                    decorationBox = { field ->
                        if (state.searchText.isBlank()) {
                            Text(
                                if (state.searchMode == SearchMode.TAG) "输入标签" else "描述人物、场景或动作",
                                color = MaterialTheme.colorScheme.secondary,
                                style = MaterialTheme.typography.bodyMedium,
                            )
                        }
                        field()
                    },
                )
                TextButton(onClick = { onMode(if (state.searchMode == SearchMode.TAG) SearchMode.TEXT else SearchMode.TAG) }) {
                    Text(if (state.searchMode == SearchMode.TAG) "标签" else "语义", color = MaterialTheme.colorScheme.secondary)
                }
                Button(
                    onClick = if (state.isSearching) onCancelSearch else onSearch,
                    enabled = if (state.isSearching) true else canSearch,
                    modifier = Modifier.size(42.dp),
                    contentPadding = PaddingValues(0.dp),
                    shape = RoundedCornerShape(50),
                ) {
                    Text(if (state.isSearching) "×" else "↑", fontSize = 20.sp)
                }
            }
        }

        if (state.queryImage != null && state.searchMode != SearchMode.TAG) {
            Spacer(Modifier.height(10.dp))
            QueryImageControl(
                query = state.queryImage,
                onPickImage = onPickImage,
                onCancelUpload = onCancelUpload,
                onRetryUpload = onRetryUpload,
                onClearImage = onClearImage,
            )
        }

        Spacer(Modifier.height(12.dp))
        TextButton(onClick = { optionsOpen = true }) { Text("☷  搜索设置", color = MaterialTheme.colorScheme.secondary) }
        state.searchStatus?.let {
            Spacer(Modifier.height(6.dp))
            Text(it, style = MaterialTheme.typography.bodySmall)
        }
        state.searchRestoreNotice?.let {
            Spacer(Modifier.height(6.dp))
            Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.secondary)
        }
        if (state.searchPhase == SearchPhase.ERROR) {
            state.searchError?.let { ErrorBanner(it) }
            TextButton(onClick = onSearch, enabled = canSearch) { Text("重试") }
        } else if (state.searchPhase == SearchPhase.CANCELLED) {
            Text("已取消", style = MaterialTheme.typography.bodySmall)
        }
        state.errorMessage?.let {
            Spacer(Modifier.height(8.dp))
            ErrorBanner(it)
        }
    }
}

@Composable
private fun LibraryChip(library: LibraryDto, selected: Boolean, onClick: () -> Unit) {
    FilterChip(
        selected = selected,
        onClick = onClick,
        label = { Text(if (library.count == null) library.name else "${library.name} (${library.count})") },
    )
}

@Composable
internal fun OriginalViewer(
    state: AppUiState,
    mediaUrl: (OriginalMediaItem) -> String,
    onIndexChanged: (Int) -> Unit,
    onClose: () -> Unit,
    onSave: () -> Unit,
    onShare: () -> Unit,
    onCancelTransfer: () -> Unit,
    onReaction: (String, RecommendationAction) -> Unit,
) {
    BackHandler(onBack = onClose)
    val viewerItems = state.viewerItems()
    val initial = state.viewerIndex?.coerceIn(viewerItems.indices) ?: 0
    val pagerState = rememberPagerState(initialPage = initial, pageCount = { viewerItems.size })
    var sheetItem by remember { mutableStateOf<OriginalMediaItem?>(null) }
    var showInfo by remember { mutableStateOf(false) }
    val rotations = remember { mutableStateMapOf<Int, Float>() }

    LaunchedEffect(pagerState) {
        snapshotFlow { pagerState.currentPage }.distinctUntilChanged().collect { page ->
            // The page boundary is handled by ViewModel.setViewerIndex against the latest list,
            // so the viewer never duplicates the pagination decision with a stale snapshot.
            onIndexChanged(page)
        }
    }

    Surface(color = androidx.compose.ui.graphics.Color.Black, modifier = Modifier.fillMaxSize()) {
        Box(Modifier.fillMaxSize()) {
            HorizontalPager(state = pagerState, modifier = Modifier.fillMaxSize()) { page ->
                val item = viewerItems[page]
                Box(
                    Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center,
                ) {
                    ZoomableOriginalImage(
                        url = mediaUrl(item),
                        contentDescription = item.name,
                        rotation = rotations[page] ?: 0f,
                        onLongPress = { sheetItem = item; showInfo = false },
                    )
                }
            }

            AnimatedVisibility(
                visible = state.transferMessage != null,
                modifier = Modifier.align(Alignment.BottomCenter),
            ) {
                Column(
                    Modifier.fillMaxWidth().background(androidx.compose.ui.graphics.Color.Black.copy(alpha = 0.75f)).padding(16.dp),
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            state.transferMessage.orEmpty(),
                            color = androidx.compose.ui.graphics.Color.White,
                            modifier = Modifier.weight(1f),
                        )
                        TextButton(onClick = onCancelTransfer) {
                            Text("取消", color = androidx.compose.ui.graphics.Color.White)
                        }
                    }
                    Spacer(Modifier.height(6.dp))
                    val fraction = state.transferFraction
                    if (fraction == null) LinearProgressIndicator(
                        modifier = Modifier.fillMaxWidth(),
                        color = androidx.compose.ui.graphics.Color.White,
                        trackColor = androidx.compose.ui.graphics.Color.White.copy(alpha = 0.24f),
                    )
                    else LinearProgressIndicator(
                        progress = { fraction },
                        modifier = Modifier.fillMaxWidth(),
                        color = androidx.compose.ui.graphics.Color.White,
                        trackColor = androidx.compose.ui.graphics.Color.White.copy(alpha = 0.24f),
                    )
                }
            }
        }
    }

    sheetItem?.let { item ->
        ModalBottomSheet(onDismissRequest = { sheetItem = null }) {
            Column(
                Modifier.fillMaxWidth().padding(horizontal = 20.dp).navigationBarsPadding(),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                val transferBusy = state.transferAttempt != null
                TextButton(
                    onClick = { sheetItem = null; onSave() },
                    enabled = !transferBusy,
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("保存", modifier = Modifier.fillMaxWidth(), style = MaterialTheme.typography.bodyLarge) }
                TextButton(
                    onClick = { sheetItem = null; onShare() },
                    enabled = !transferBusy,
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("分享", modifier = Modifier.fillMaxWidth(), style = MaterialTheme.typography.bodyLarge) }
                (item as? RecommendationItem)?.let { recommendation ->
                    val reactionPending = recommendation.itemId in state.recommendations.pendingReactionItemIds
                    val reaction = state.recommendations.reactions[recommendation.itemId]
                    TextButton(
                        onClick = {
                            sheetItem = null
                            onReaction(recommendation.itemId, RecommendationAction.LIKE)
                        },
                        enabled = !reactionPending && reaction != RecommendationAction.LIKE,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Text(
                            if (reaction == RecommendationAction.LIKE) "已喜欢" else "喜欢",
                            modifier = Modifier.fillMaxWidth(),
                            style = MaterialTheme.typography.bodyLarge,
                        )
                    }
                    TextButton(
                        onClick = {
                            sheetItem = null
                            onReaction(recommendation.itemId, RecommendationAction.DISLIKE)
                        },
                        enabled = !reactionPending && reaction != RecommendationAction.DISLIKE,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Text(
                            if (reaction == RecommendationAction.DISLIKE) "已不喜欢" else "不喜欢",
                            modifier = Modifier.fillMaxWidth(),
                            style = MaterialTheme.typography.bodyLarge,
                        )
                    }
                }
                TextButton(
                    onClick = {
                        val page = pagerState.currentPage
                        rotations[page] = ((rotations[page] ?: 0f) + 90f) % 360f
                    },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("旋转", modifier = Modifier.fillMaxWidth(), style = MaterialTheme.typography.bodyLarge) }
                TextButton(
                    onClick = { showInfo = !showInfo },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("详细信息", modifier = Modifier.fillMaxWidth(), style = MaterialTheme.typography.bodyLarge) }
                if (showInfo) {
                    Column(
                        Modifier.fillMaxWidth().padding(vertical = 8.dp),
                        verticalArrangement = Arrangement.spacedBy(6.dp),
                    ) {
                        Text("文件名：${item.name.ifBlank { "未命名" }}", style = MaterialTheme.typography.bodyMedium)
                        if (item.width != null && item.height != null) {
                            Text("尺寸：${item.width} × ${item.height}", style = MaterialTheme.typography.bodyMedium)
                        }
                        item.score?.let { Text("评分：${DecimalFormat("0.000").format(it)}", style = MaterialTheme.typography.bodyMedium) }
                        item.libraryName?.let { Text("图库：$it", style = MaterialTheme.typography.bodyMedium) }
                        item.sizeBytes?.let { Text("大小：${formatBytes(it)}", style = MaterialTheme.typography.bodyMedium) }
                        if (item.tags.isNotEmpty()) {
                            Text("标签：${item.tags.joinToString("、")}", style = MaterialTheme.typography.bodyMedium)
                        }
                    }
                }
                Spacer(Modifier.height(16.dp))
            }
        }
    }
}

@Composable
private fun ZoomableOriginalImage(url: String, contentDescription: String, rotation: Float = 0f, onLongPress: () -> Unit = {}) {
    val context = LocalContext.current
    var scale by remember(url) { mutableFloatStateOf(1f) }
    var offset by remember(url) { mutableStateOf(Offset.Zero) }
    var viewport by remember(url) { mutableStateOf(IntSize.Zero) }

    fun clampOffset(candidate: Offset, currentScale: Float): Offset {
        val maxX = viewport.width * (currentScale - 1f) / 2f
        val maxY = viewport.height * (currentScale - 1f) / 2f
        return Offset(candidate.x.coerceIn(-maxX, maxX), candidate.y.coerceIn(-maxY, maxY))
    }

    SubcomposeAsyncImage(
        model = ImageRequest.Builder(context).data(url).crossfade(true).build(),
        contentDescription = contentDescription,
        contentScale = ContentScale.Fit,
        modifier = Modifier
            .fillMaxSize()
            .testTag(ORIGINAL_VIEWER_IMAGE_TAG)
            .onSizeChanged { viewport = it }
            .graphicsLayer {
                scaleX = scale
                scaleY = scale
                translationX = offset.x
                translationY = offset.y
                rotationZ = rotation
            }
            .pointerInput(url) {
                detectTapGestures(
                    onDoubleTap = {
                        scale = if (scale > 1f) 1f else 2.5f
                        if (scale == 1f) offset = Offset.Zero
                    },
                    onLongPress = { onLongPress() },
                )
            }
            .pointerInput(url) {
                awaitEachGesture {
                    awaitFirstDown(requireUnconsumed = false)
                    do {
                        val event = awaitPointerEvent()
                        val pressedCount = event.changes.count { it.pressed }
                        if (pressedCount >= 2 || scale > 1f) {
                            val newScale = (scale * event.calculateZoom()).coerceIn(1f, 6f)
                            val newOffset = if (newScale == 1f) Offset.Zero
                            else clampOffset(offset + event.calculatePan(), newScale)
                            scale = newScale
                            offset = newOffset
                            event.changes.forEach { it.consume() }
                        }
                    } while (event.changes.any { it.pressed })
                }
            },
    ) {
        when (painter.state) {
            is coil.compose.AsyncImagePainter.State.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = androidx.compose.ui.graphics.Color.White)
            }
            is coil.compose.AsyncImagePainter.State.Error -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text("无法显示此原图，可尝试保存后用其他应用打开", color = androidx.compose.ui.graphics.Color.White)
            }
            else -> SubcomposeAsyncImageContent()
        }
    }
}

@Composable
private fun ErrorBanner(message: String) {
    Surface(color = MaterialTheme.colorScheme.error.copy(alpha = 0.10f), shape = RoundedCornerShape(10.dp)) {
        Text(
            message,
            modifier = Modifier.fillMaxWidth().padding(12.dp),
            color = MaterialTheme.colorScheme.error,
            style = MaterialTheme.typography.bodyMedium,
        )
    }
}

private fun SearchMode.label(): String = when (this) {
    SearchMode.TEXT -> "文字"
    SearchMode.TAG -> "标签"
    SearchMode.IMAGE -> "图片"
    SearchMode.COMBINED -> "图文"
}

internal fun formatBytes(bytes: Long): String {
    if (bytes < 1024) return "$bytes B"
    val units = arrayOf("KiB", "MiB", "GiB", "TiB")
    var value = bytes.toDouble()
    var index = -1
    do {
        value /= 1024.0
        index++
    } while (value >= 1024.0 && index < units.lastIndex)
    return "${DecimalFormat("0.##").format(value)} ${units[index]}"
}

private fun formatTime(millis: Long): String =
    SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.getDefault()).format(java.util.Date(millis))

/**
 * Transfer progress for a still-running save or share. It stays visible after the viewer closes,
 * so the user always has a target name, a progress hint and a cancel entry.
 */
@Composable
internal fun TransferStatusBar(
    message: String,
    fraction: Float?,
    onCancel: () -> Unit,
) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        color = MaterialTheme.colorScheme.surfaceVariant,
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(message, style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Spacer(Modifier.height(4.dp))
                if (fraction == null) {
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                } else {
                    LinearProgressIndicator(progress = { fraction }, modifier = Modifier.fillMaxWidth())
                }
            }
            Spacer(Modifier.width(8.dp))
            TextButton(onClick = onCancel) { Text("取消") }
        }
    }
}

/**
 * Small snapshot of the media the save picker was launched for. Only identity and the fields the
 * copy needs are kept so a configuration change can still bind the transfer to the original item
 * without ever storing the whole result list.
 */
internal data class PendingSaveTarget(
    val source: ViewerSource,
    val mediaId: String,
    val name: String,
    val contentType: String?,
    val recommendationItemId: String?,
) {
    fun resolve(state: AppUiState): OriginalMediaItem? {
        if (state.phase != ConnectionPhase.READY) return null
        val items = when (source) {
            ViewerSource.SEARCH -> state.results
            ViewerSource.RECOMMENDATIONS -> state.recommendations.items
        }
        return items.firstOrNull {
            it.mediaId == mediaId &&
                (it !is RecommendationItem || it.itemId == recommendationItemId)
        }
    }

    companion object {
        fun of(item: OriginalMediaItem, source: ViewerSource?): PendingSaveTarget = PendingSaveTarget(
            source = source ?: ViewerSource.SEARCH,
            mediaId = item.mediaId,
            name = item.name,
            contentType = item.contentType,
            recommendationItemId = (item as? RecommendationItem)?.itemId,
        )
    }
}

private val PendingSaveTargetSaver: Saver<PendingSaveTarget?, Any> = Saver(
    save = { target ->
        target?.let {
            arrayListOf(
                it.source.name,
                it.mediaId,
                it.name,
                it.contentType.orEmpty(),
                it.recommendationItemId.orEmpty(),
            )
        }
    },
    restore = { saved ->
        @Suppress("UNCHECKED_CAST")
        val values = saved as List<String>
        if (values.isEmpty()) {
            null
        } else {
            PendingSaveTarget(
                source = runCatching { ViewerSource.valueOf(values[0]) }.getOrDefault(ViewerSource.SEARCH),
                mediaId = values[1],
                name = values[2],
                contentType = values[3].ifBlank { null },
                recommendationItemId = values[4].ifBlank { null },
            )
        }
    },
)

internal const val ORIGINAL_VIEWER_IMAGE_TAG = "original-viewer-image"
internal const val RECOMMENDATION_GRID_TAG = "recommendation-grid"
internal const val APP_SNACKBAR_HOST_TAG = "app-snackbar-host"
