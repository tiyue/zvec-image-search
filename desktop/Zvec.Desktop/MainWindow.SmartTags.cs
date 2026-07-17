using System.Collections.ObjectModel;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media.Imaging;
using Zvec.Desktop.Collections;
using Zvec.Desktop.Models;

namespace Zvec.Desktop;

public partial class MainWindow
{
    private static readonly HashSet<string> SmartTagCommands = new(
        [
            "auto_tag_estimate",
            "auto_tag",
            "auto_tag_pending",
            "auto_tag_review",
            "auto_tag_review_batch",
            "auto_tag_review_undo",
            "index_and_auto_tag",
            "folder_tag_backfill",
            "metadata_backfill",
            "tag_alias_list",
            "tag_alias_upsert",
            "tag_alias_delete",
        ],
        StringComparer.Ordinal
    );
    private const int AutoTagReviewPageSize = 100;
    private static readonly SemaphoreSlim AutoTagThumbnailGate = new(3, 3);

    private readonly ObservableCollection<LibraryChoiceItem> _smartTagLibraryChoices = [];
    private readonly ObservableCollection<FolderTagPreview> _folderTagPreviews = [];
    private readonly RangeObservableCollection<AutoTagReviewItem> _autoTagReviewItems = [];
    private readonly ObservableCollection<TagAliasEntry> _tagAliasEntries = [];
    private string? _autoTagReviewLibraryId;
    private int _autoTagReviewOffset;
    private int _autoTagReviewPendingCount;
    private bool _autoTagReviewHasMore;
    private bool _autoTagUndoAvailable;
    private CancellationTokenSource _autoTagThumbnailCancellation = new();

    private void SmartTagsTab_Loaded(object sender, RoutedEventArgs e)
    {
        SmartTagLibraryComboBox.ItemsSource = _smartTagLibraryChoices;
        FolderTagPreviewListBox.ItemsSource = _folderTagPreviews;
        AutoTagReviewListBox.ItemsSource = _autoTagReviewItems;
        TagAliasListBox.ItemsSource = _tagAliasEntries;
        SmartTagScopeComboBox.SelectedIndex = Math.Max(0, SmartTagScopeComboBox.SelectedIndex);
        RefreshAutoTagTaskModelChoices();
        RefreshSmartTagLibraryChoices();
        UpdateAutoTagReviewPageControls();
    }

    private void RefreshSmartTagLibraryChoices()
    {
        var previousId = (SmartTagLibraryComboBox.SelectedItem as LibraryChoiceItem)?.LibraryId;
        _smartTagLibraryChoices.Clear();
        if (_config is null)
        {
            SmartTagLibraryComboBox.SelectedItem = null;
            return;
        }

        foreach (var library in _config.EnabledLibraries)
        {
            _smartTagLibraryChoices.Add(new LibraryChoiceItem
            {
                LibraryId = library.Id,
                Name = library.Name,
                IsDefault = string.Equals(
                    library.Id,
                    _config.DefaultLibraryId,
                    StringComparison.OrdinalIgnoreCase
                ),
            });
        }

        SmartTagLibraryComboBox.SelectedItem = _smartTagLibraryChoices.FirstOrDefault(item =>
            string.Equals(
                item.LibraryId,
                previousId ?? _config.DefaultLibraryId,
                StringComparison.OrdinalIgnoreCase
            )
        ) ?? _smartTagLibraryChoices.FirstOrDefault();
    }

    private async void SmartTagEstimate_Click(object sender, RoutedEventArgs e)
    {
        if (!TryReadAutoTagOptions(requireExternalConfirmation: false, out var options))
        {
            return;
        }

        var request = new AutoTagEstimateRequest
        {
            LibraryId = options.LibraryId,
            Scope = options.Scope,
            Model = options.Model,
            MaxImages = options.MaxImages,
            MaxBudgetCny = options.MaxBudgetCny,
            ExternalProcessingConfirmed = ExternalProcessingConfirmCheckBox.IsChecked == true,
        };
        BeginSmartTagProgress("正在统计候选、缓存命中和文件夹标签…");
        var job = await RunBackendSubmissionAsync(
            "正在预览自动标签与费用…",
            "auto_tag_estimate",
            async backend => await backend.ApiClient.EstimateAutoTagsAsync(request)
        );
        if (job is null)
        {
            ResetSmartTagProgress("自动标注预览未完成");
            ShowValidation("智能整理需要常驻后端，请先确认原生 Python 后端可用。");
            return;
        }
        if (!HandleBackendJob(job, "自动标注预览已生成"))
        {
            ResetSmartTagProgress("自动标注预览未完成");
            return;
        }

        try
        {
            ShowAutoTagEstimate(await job.DeserializeResultAsync<AutoTagEstimateResult>());
        }
        catch (Exception exception)
        {
            ShowOperationError("无法读取自动标注预览", exception);
        }
    }

    private async void SmartTagStart_Click(object sender, RoutedEventArgs e)
    {
        if (!TryReadAutoTagOptions(requireExternalConfirmation: true, out var options))
        {
            return;
        }

        if (!ConfirmAutoTagCost(
            options.Model,
            options.MaxImages,
            options.MaxBudgetCny,
            "将最多",
            "确认开始自动标注"
        ))
        {
            return;
        }

        var request = new AutoTagRunRequest
        {
            LibraryId = options.LibraryId,
            Scope = options.Scope,
            Model = options.Model,
            MaxImages = options.MaxImages,
            MaxBudgetCny = options.MaxBudgetCny,
            ExternalProcessingConfirmed = true,
        };
        BeginSmartTagProgress("正在提交自动标注任务…");
        var job = await RunBackendSubmissionAsync(
            "正在生成自动标签建议…",
            "auto_tag",
            async backend => await backend.ApiClient.StartAutoTagAsync(request),
            _ => ExternalProcessingConfirmCheckBox.IsChecked = false
        );
        if (job is null)
        {
            ResetSmartTagProgress("自动标注任务未完成");
            ShowValidation("智能整理需要常驻后端，请先确认原生 Python 后端可用。");
            return;
        }
        if (!HandleBackendJob(job, "自动标签建议已生成"))
        {
            ResetSmartTagProgress("自动标注任务未完成");
            return;
        }

        try
        {
            var result = await job.DeserializeResultAsync<AutoTagRunSummaryResult>();
            ShowAutoTagRun(result);
            await LoadAutoTagReviewPageAsync(
                options.LibraryId,
                requestedOffset: 0,
                preferPreviousIfEmpty: false
            );
        }
        catch (Exception exception)
        {
            ShowOperationError("无法读取自动标签建议", exception);
        }
    }

    private async void FolderTagBackfill_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetSmartTagLibrary(out var libraryId))
        {
            return;
        }

        var answer = MessageBox.Show(
            this,
            "将重新扫描当前图库，为历史图片补全清洗后的文件夹标签。" +
            "该操作完全在本地完成，不调用视觉模型，也不会覆盖人工标签。是否继续？",
            "补全文件夹标签",
            MessageBoxButton.YesNo,
            MessageBoxImage.Information
        );
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }

        var request = new FolderTagBackfillRequest
        {
            LibraryId = libraryId,
            Recursive = true,
            VerifyHash = false,
        };
        BeginSmartTagProgress("正在本地补全文件夹标签…");
        var job = await RunBackendSubmissionAsync(
            "正在补全文件夹标签…",
            "folder_tag_backfill",
            async backend => await backend.ApiClient.BackfillFolderTagsAsync(request)
        );
        if (job is null)
        {
            ResetSmartTagProgress("文件夹标签补全未完成");
            ShowValidation("文件夹标签补全需要常驻后端，请先确认原生 Python 后端可用。");
            return;
        }
        if (!HandleBackendJob(job, "文件夹标签补全完成"))
        {
            ResetSmartTagProgress("文件夹标签补全未完成");
            return;
        }

        CompleteSmartTagProgress("文件夹标签补全完成（未调用视觉模型）");
        SmartTagEstimateSummaryTextBlock.Text =
            "历史图片的文件夹标签已经重新计算；人工标签和已接受的模型标签保持不变。";
    }

    private async void MetadataBackfill_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetSmartTagLibrary(out var libraryId))
        {
            return;
        }
        if (!TryParseInteger(SmartTagMaxImagesTextBox.Text, 1, 10_000, out var maxImages))
        {
            ShowValidation("最多处理图片必须是 1 到 10,000 之间的整数。");
            return;
        }
        if (_modelConfigurationRestartPending)
        {
            ShowValidation("新的模型配置尚未生效，请等待当前任务完成和后端自动重启。");
            return;
        }

        var answer = MessageBox.Show(
            this,
            $"将为最多 {maxImages} 张待处理图片生成描述向量。" +
            "仅发送已审核标签和短描述文本到阿里云 embedding 模型，不发送原图；" +
            "已完成项目会自动跳过，失败项目会保留到下次继续。是否继续？",
            "生成描述向量",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning
        );
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }

        BeginSmartTagProgress("正在提交描述向量回填任务…");
        var request = new MetadataBackfillRequest
        {
            LibraryId = libraryId,
            MaxImages = maxImages,
        };
        var job = await RunBackendSubmissionAsync(
            "正在生成描述向量…",
            "metadata_backfill",
            async backend =>
            {
                if (!backend.SupportsMetadataEmbeddingBackfill)
                {
                    throw new InvalidOperationException(
                        "当前常驻后端不支持描述向量，请更新桌面应用后重试。"
                    );
                }
                return await backend.ApiClient.BackfillMetadataEmbeddingsAsync(request);
            }
        );
        if (job is null)
        {
            ResetSmartTagProgress("描述向量任务未完成");
            ShowValidation("生成描述向量需要常驻后端，请先确认原生 Python 后端可用。");
            return;
        }
        if (!HandleBackendJob(job, "描述向量生成完成"))
        {
            ResetSmartTagProgress("描述向量任务未完成");
            return;
        }

        try
        {
            var result = await job.DeserializeResultAsync<MetadataBackfillResult>();
            var summary =
                $"扫描 {result.Scanned} 张，生成 {result.Succeeded} 张，" +
                $"已是最新 {result.AlreadyCurrent} 张，跳过空描述 {result.SkippedEmpty} 张，" +
                $"失败 {result.Failed} 张，剩余 {result.Remaining} 张；" +
                $"API 请求 {result.ApiRequests} 次。";
            SmartTagEstimateSummaryTextBlock.Text = summary;
            CompleteSmartTagProgress(result.Remaining > 0
                ? "本批描述向量已完成，可稍后继续"
                : "描述向量已全部更新");
            AppendLog($"描述向量回填完成：{summary}", isError: result.Failed > 0);
        }
        catch (Exception exception)
        {
            ShowOperationError("无法读取描述向量任务结果", exception);
        }
    }

    private async void RefreshTagAliases_Click(object sender, RoutedEventArgs e) =>
        await LoadTagAliasesAsync();

    private void NewTagAlias_Click(object sender, RoutedEventArgs e)
    {
        TagAliasListBox.SelectedItem = null;
        TagAliasCanonicalTextBox.Clear();
        TagAliasAliasesTextBox.Clear();
        TagAliasCanonicalTextBox.Focus();
    }

    private void TagAliasListBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (TagAliasListBox.SelectedItem is not TagAliasEntry entry)
        {
            return;
        }
        TagAliasCanonicalTextBox.Text = entry.EffectiveCanonicalName;
        TagAliasAliasesTextBox.Text = entry.EditAliasesText;
    }

    private async void SaveTagAlias_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetSmartTagLibrary(out var libraryId))
        {
            return;
        }
        var canonicalName = TagAliasCanonicalTextBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(canonicalName))
        {
            ShowValidation("请填写规范名称，例如“雷电将军”。");
            return;
        }
        var aliases = SplitTokens(TagAliasAliasesTextBox.Text)
            .Where(alias => !string.Equals(alias, canonicalName, StringComparison.Ordinal))
            .ToList();
        if (aliases.Count == 0)
        {
            ShowValidation("请至少填写一个不同于规范名称的别名，例如“雷神”或“影”。");
            return;
        }

        var request = new TagAliasUpsertRequest
        {
            LibraryId = libraryId,
            CanonicalName = canonicalName,
            Aliases = aliases,
        };
        BeginSmartTagProgress("正在保存别名词典…");
        var job = await RunBackendSubmissionAsync(
            "正在保存别名词典…",
            "tag_alias_upsert",
            async backend => await backend.ApiClient.UpsertTagAliasAsync(request)
        );
        if (job is null)
        {
            ResetSmartTagProgress("别名词典未保存");
            ShowValidation("保存别名词典需要常驻后端，请先确认原生 Python 后端可用。");
            return;
        }
        if (!HandleBackendJob(job, "别名词典已保存"))
        {
            ResetSmartTagProgress("别名词典未保存");
            return;
        }

        try
        {
            _ = await job.DeserializeResultAsync<TagAliasMutationResult>();
            CompleteSmartTagProgress("别名词典已保存");
            await LoadTagAliasesAsync();
        }
        catch (Exception exception)
        {
            ShowOperationError("无法读取别名保存结果", exception);
        }
    }

    private async void DeleteTagAlias_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetSmartTagLibrary(out var libraryId))
        {
            return;
        }
        var entry = (sender as FrameworkElement)?.DataContext as TagAliasEntry ??
            TagAliasListBox.SelectedItem as TagAliasEntry;
        if (entry is null || string.IsNullOrWhiteSpace(entry.EffectiveCanonicalName))
        {
            ShowValidation("请先选择需要删除的别名关系。");
            return;
        }
        var answer = MessageBox.Show(
            this,
            $"确定删除“{entry.EffectiveCanonicalName}”及其别名关系吗？标签本身不会被删除。",
            "删除别名关系",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning
        );
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }

        var request = new TagAliasDeleteRequest
        {
            LibraryId = libraryId,
            CanonicalName = entry.EffectiveCanonicalName,
        };
        BeginSmartTagProgress("正在删除别名关系…");
        var job = await RunBackendSubmissionAsync(
            "正在删除别名关系…",
            "tag_alias_delete",
            async backend => await backend.ApiClient.DeleteTagAliasAsync(request)
        );
        if (job is null)
        {
            ResetSmartTagProgress("别名关系未删除");
            ShowValidation("删除别名关系需要常驻后端，请先确认原生 Python 后端可用。");
            return;
        }
        if (!HandleBackendJob(job, "别名关系已删除"))
        {
            ResetSmartTagProgress("别名关系未删除");
            return;
        }

        try
        {
            _ = await job.DeserializeResultAsync<TagAliasMutationResult>();
            NewTagAlias_Click(sender, e);
            CompleteSmartTagProgress("别名关系已删除");
            await LoadTagAliasesAsync();
        }
        catch (Exception exception)
        {
            ShowOperationError("无法读取别名删除结果", exception);
        }
    }

    private async Task<bool> LoadTagAliasesAsync()
    {
        if (!TryGetSmartTagLibrary(out var libraryId))
        {
            return false;
        }
        var request = new TagAliasListRequest { LibraryId = libraryId };
        BeginSmartTagProgress("正在加载别名词典…");
        var job = await RunBackendSubmissionAsync(
            "正在加载别名词典…",
            "tag_alias_list",
            async backend => await backend.ApiClient.ListTagAliasesAsync(request)
        );
        if (job is null)
        {
            ResetSmartTagProgress("别名词典加载失败");
            ShowValidation("加载别名词典需要常驻后端，请先确认原生 Python 后端可用。");
            return false;
        }
        if (!HandleBackendJob(job, "别名词典已加载"))
        {
            ResetSmartTagProgress("别名词典加载失败");
            return false;
        }

        try
        {
            var result = await job.DeserializeResultAsync<TagAliasListResult>();
            _tagAliasEntries.Clear();
            foreach (var entry in result.EffectiveAliases
                .Where(entry => !string.IsNullOrWhiteSpace(entry.EffectiveCanonicalName))
                .OrderBy(entry => entry.EffectiveCanonicalName, StringComparer.CurrentCulture))
            {
                _tagAliasEntries.Add(entry);
            }
            TagAliasSummaryTextBlock.Text = _tagAliasEntries.Count == 0
                ? "共用词典尚未建立别名关系。"
                : $"共用词典已加载 {_tagAliasEntries.Count} 组关系；搜索和审核统一使用规范名称。";
            CompleteSmartTagProgress("别名词典已加载");
            return true;
        }
        catch (Exception exception)
        {
            ResetSmartTagProgress("别名词典加载失败");
            ShowOperationError("无法读取别名词典", exception);
            return false;
        }
    }

    private void SelectAllLowRiskAutoTags_Click(object sender, RoutedEventArgs e)
    {
        foreach (var item in _autoTagReviewItems)
        {
            item.IsBatchSelected = item.CanBatchAccept;
        }
        UpdateAutoTagReviewPageControls();
    }

    private void ClearLowRiskAutoTagSelection_Click(object sender, RoutedEventArgs e)
    {
        foreach (var item in _autoTagReviewItems)
        {
            item.IsBatchSelected = false;
        }
        UpdateAutoTagReviewPageControls();
    }

    private void LowRiskAutoTagSelection_Click(object sender, RoutedEventArgs e) =>
        UpdateAutoTagReviewPageControls();

    private async void AutoTagThumbnail_Loaded(object sender, RoutedEventArgs e)
    {
        if (sender is Image image)
        {
            await EnsureAutoTagThumbnailAsync(
                image,
                _autoTagThumbnailCancellation.Token
            );
        }
    }

    private async void AutoTagThumbnail_DataContextChanged(
        object sender,
        DependencyPropertyChangedEventArgs e)
    {
        if (sender is Image { IsLoaded: true } image)
        {
            await EnsureAutoTagThumbnailAsync(
                image,
                _autoTagThumbnailCancellation.Token
            );
        }
    }

    private static async Task EnsureAutoTagThumbnailAsync(
        Image image,
        CancellationToken cancellationToken)
    {
        if (
            image.DataContext is not AutoTagReviewItem item ||
            item.Thumbnail is not null ||
            !item.TryBeginThumbnailLoad())
        {
            return;
        }

        var enteredGate = false;
        try
        {
            // Only realized (visible) virtualized cards reach this handler. Bound decoding
            // concurrency so fast scrolling cannot exhaust the thread pool or image memory.
            await AutoTagThumbnailGate.WaitAsync(cancellationToken);
            enteredGate = true;
            var thumbnail = await Task.Run(
                () => LoadAutoTagReviewThumbnail(item.SourcePath, cancellationToken),
                cancellationToken
            );
            if (thumbnail is not null && !cancellationToken.IsCancellationRequested)
            {
                item.Thumbnail = thumbnail;
            }
        }
        catch (OperationCanceledException)
        {
            // Replacing the page cancels previews that are no longer visible or relevant.
        }
        finally
        {
            if (enteredGate)
            {
                AutoTagThumbnailGate.Release();
            }
        }
    }

    private static BitmapSource? LoadAutoTagReviewThumbnail(
        string sourcePath,
        CancellationToken cancellationToken)
    {
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (string.IsNullOrWhiteSpace(sourcePath) || !File.Exists(sourcePath))
            {
                return null;
            }
            var (pixelWidth, pixelHeight) = ReadAutoTagImageDimensions(
                sourcePath,
                cancellationToken
            );
            using var stream = new FileStream(
                sourcePath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete
            );
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.CreateOptions = BitmapCreateOptions.IgnoreColorProfile;
            var decodeSize = AutoTagThumbnailSizing.GetDecodeDimensions(
                pixelWidth,
                pixelHeight
            );
            if (decodeSize.DecodePixelHeight > 0)
            {
                bitmap.DecodePixelHeight = decodeSize.DecodePixelHeight;
            }
            else
            {
                bitmap.DecodePixelWidth = decodeSize.DecodePixelWidth;
            }
            bitmap.StreamSource = stream;
            bitmap.EndInit();
            cancellationToken.ThrowIfCancellationRequested();
            bitmap.Freeze();
            return bitmap;
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception)
        {
            // Missing, locked, or unsupported images keep the lightweight placeholder.
            return null;
        }
    }

    private static (int Width, int Height) ReadAutoTagImageDimensions(
        string sourcePath,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        using var stream = new FileStream(
            sourcePath,
            FileMode.Open,
            FileAccess.Read,
            FileShare.ReadWrite | FileShare.Delete
        );
        var decoder = BitmapDecoder.Create(
            stream,
            BitmapCreateOptions.DelayCreation | BitmapCreateOptions.IgnoreColorProfile,
            BitmapCacheOption.None
        );
        var frame = decoder.Frames.FirstOrDefault();
        return frame is null
            ? (AutoTagThumbnailSizing.LongestEdge, AutoTagThumbnailSizing.LongestEdge)
            : (Math.Max(1, frame.PixelWidth), Math.Max(1, frame.PixelHeight));
    }

    private async void AcceptSelectedLowRiskAutoTags_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetLoadedAutoTagReviewLibrary(out var libraryId))
        {
            return;
        }

        var selected = _autoTagReviewItems
            .Where(item => item.IsBatchSelected && item.CanBatchAccept)
            .ToList();
        if (selected.Count == 0)
        {
            ShowValidation("请先勾选至少一张含低风险新增标签的图片。");
            return;
        }

        var acceptedTags = selected.ToDictionary(
            item => item.ProposalId,
            item => item.BuildBatchAcceptedTags(),
            StringComparer.Ordinal
        );
        if (acceptedTags.Values.Any(tags => tags.Count == 0))
        {
            ShowValidation("所选项目没有可安全批量写入的普通标签，请逐项审核。");
            return;
        }

        var answer = MessageBox.Show(
            this,
            $"将为 {selected.Count} 张图片接受低风险普通标签。" +
            "真人、Cosplayer、角色和作品等身份标签不会被批量写入。是否继续？",
            "批量接受低风险标签",
            MessageBoxButton.YesNo,
            MessageBoxImage.Information
        );
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }

        var request = new AutoTagBatchReviewRequest
        {
            LibraryId = libraryId,
            ProposalIds = selected.Select(item => item.ProposalId).ToList(),
            AcceptedTagsByProposal = acceptedTags,
        };
        BeginSmartTagProgress("正在批量应用低风险标签…");
        var job = await RunBackendSubmissionAsync(
            "正在批量应用低风险标签…",
            "auto_tag_review_batch",
            async backend => await backend.ApiClient.SubmitAutoTagBatchReviewAsync(request)
        );
        if (job is null)
        {
            ResetSmartTagProgress("低风险标签未批量应用");
            ShowValidation("批量审核需要常驻后端，请先确认原生 Python 后端可用。");
            return;
        }
        if (!HandleBackendJob(job, "低风险标签已批量应用"))
        {
            ResetSmartTagProgress("低风险标签未批量应用");
            return;
        }

        try
        {
            var result = await job.DeserializeResultAsync<AutoTagBatchReviewResult>();
            _autoTagUndoAvailable = result.UndoAvailable;
            await LoadAutoTagReviewPageAsync(
                libraryId,
                _autoTagReviewOffset,
                preferPreviousIfEmpty: true
            );
            AutoTagReviewSummaryTextBlock.Text =
                $"刚刚批量接受 {result.Accepted} 项，更新 {result.Updated} 张图片；" +
                $"身份标签已排除 {result.EffectiveIdentityExcludedCount} 项。";
            CompleteSmartTagProgress("低风险标签已批量应用，可撤销最近一次批量操作");
            UpdateAutoTagReviewPageControls();
        }
        catch (Exception exception)
        {
            ShowOperationError("无法读取批量审核结果", exception);
        }
    }

    private void RejectAllAutoTags_Click(object sender, RoutedEventArgs e)
    {
        foreach (var item in _autoTagReviewItems)
        {
            item.Decision = "reject";
        }
    }

    private async void ApplyAutoTagReviewFilters_Click(object sender, RoutedEventArgs e)
    {
        if (!ConfirmDiscardAutoTagReviewChanges() || !TryGetSmartTagLibrary(out var libraryId))
        {
            return;
        }
        await LoadAutoTagReviewPageAsync(
            libraryId,
            requestedOffset: 0,
            preferPreviousIfEmpty: false
        );
    }

    private async void ClearAutoTagReviewFilters_Click(object sender, RoutedEventArgs e)
    {
        if (!ConfirmDiscardAutoTagReviewChanges() || !TryGetSmartTagLibrary(out var libraryId))
        {
            return;
        }
        LatestIndexOnlyCheckBox.IsChecked = false;
        AutoTagCharacterFilterTextBox.Clear();
        AutoTagWorkFilterTextBox.Clear();
        AutoTagActionFilterTextBox.Clear();
        AutoTagExpressionFilterTextBox.Clear();
        AutoTagReviewStateComboBox.SelectedIndex = 0;
        await LoadAutoTagReviewPageAsync(
            libraryId,
            requestedOffset: 0,
            preferPreviousIfEmpty: false
        );
    }

    private async void UndoAutoTagBatch_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetLoadedAutoTagReviewLibrary(out var libraryId))
        {
            return;
        }
        if (!_autoTagUndoAvailable)
        {
            ShowValidation("当前没有可撤销的批量审核操作。");
            return;
        }
        var answer = MessageBox.Show(
            this,
            "将撤销当前图库最近一次批量接受操作，并恢复对应待审核建议。是否继续？",
            "撤销最近一次批量操作",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning
        );
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }

        var request = new AutoTagReviewUndoRequest { LibraryId = libraryId };
        BeginSmartTagProgress("正在撤销最近一次批量操作…");
        var job = await RunBackendSubmissionAsync(
            "正在撤销最近一次批量操作…",
            "auto_tag_review_undo",
            async backend => await backend.ApiClient.UndoAutoTagBatchReviewAsync(request)
        );
        if (job is null)
        {
            ResetSmartTagProgress("批量操作未撤销");
            ShowValidation("撤销批量操作需要常驻后端，请先确认原生 Python 后端可用。");
            return;
        }
        if (!HandleBackendJob(job, "最近一次批量操作已撤销"))
        {
            ResetSmartTagProgress("批量操作未撤销");
            return;
        }

        try
        {
            var result = await job.DeserializeResultAsync<AutoTagReviewUndoResult>();
            _autoTagUndoAvailable = result.UndoAvailable;
            await LoadAutoTagReviewPageAsync(
                libraryId,
                requestedOffset: 0,
                preferPreviousIfEmpty: false
            );
            AutoTagReviewSummaryTextBlock.Text = result.Undone
                ? $"已撤销最近一次批量操作，恢复 {result.Updated} 张图片。"
                : "后端没有找到可撤销的批量操作。";
            CompleteSmartTagProgress(result.Undone
                ? "最近一次批量操作已撤销"
                : "没有可撤销的批量操作");
            UpdateAutoTagReviewPageControls();
        }
        catch (Exception exception)
        {
            ShowOperationError("无法读取撤销结果", exception);
        }
    }

    private async void RefreshAutoTagReview_Click(object sender, RoutedEventArgs e)
    {
        if (!ConfirmDiscardAutoTagReviewChanges())
        {
            return;
        }
        if (!TryGetSmartTagLibrary(out var libraryId))
        {
            return;
        }
        var offset = string.Equals(
            libraryId,
            _autoTagReviewLibraryId,
            StringComparison.OrdinalIgnoreCase
        )
            ? _autoTagReviewOffset
            : 0;
        await LoadAutoTagReviewPageAsync(
            libraryId,
            offset,
            preferPreviousIfEmpty: true
        );
    }

    private async void PreviousAutoTagReviewPage_Click(object sender, RoutedEventArgs e)
    {
        if (!ConfirmDiscardAutoTagReviewChanges())
        {
            return;
        }
        if (!TryGetSmartTagLibrary(out var libraryId))
        {
            return;
        }
        await LoadAutoTagReviewPageAsync(
            libraryId,
            Math.Max(0, _autoTagReviewOffset - AutoTagReviewPageSize),
            preferPreviousIfEmpty: false
        );
    }

    private async void NextAutoTagReviewPage_Click(object sender, RoutedEventArgs e)
    {
        if (!ConfirmDiscardAutoTagReviewChanges())
        {
            return;
        }
        if (!TryGetSmartTagLibrary(out var libraryId))
        {
            return;
        }
        await LoadAutoTagReviewPageAsync(
            libraryId,
            _autoTagReviewOffset + AutoTagReviewPageSize,
            preferPreviousIfEmpty: true
        );
    }

    private bool ConfirmDiscardAutoTagReviewChanges()
    {
        if (!_autoTagReviewItems.Any(item => item.HasUnsavedChanges))
        {
            return true;
        }
        return MessageBox.Show(
            this,
            "当前批次有尚未提交的审核修改。切换或刷新后，这些修改会丢失。是否继续？",
            "放弃未提交的审核修改",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning
        ) == MessageBoxResult.Yes;
    }

    private async void SubmitAutoTagReview_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetLoadedAutoTagReviewLibrary(out var libraryId))
        {
            return;
        }

        var reviewedItems = _autoTagReviewItems
            .Where(item => item.Decision is "accept" or "reject" or "manual")
            .ToList();
        if (reviewedItems.Count == 0)
        {
            ShowValidation("请至少把一项建议设为“接受”或“拒绝”。");
            return;
        }

        var decisions = new List<AutoTagReviewDecision>(reviewedItems.Count);
        foreach (var item in reviewedItems)
        {
            var acceptedTags = item.Decision is "accept" or "manual"
                ? item.BuildAcceptedTags(SplitTokens)
                : [];
            var confirmedIdentityTags = item.Decision == "accept"
                ? item.BuildConfirmedIdentityTags()
                : [];
            if ((item.Decision is "accept" or "manual") && acceptedTags.Count == 0)
            {
                ShowValidation($"“{item.RelativePath}”没有可写入的标签。");
                return;
            }
            decisions.Add(new AutoTagReviewDecision
            {
                ProposalId = item.ProposalId,
                Decision = item.Decision,
                AcceptedTags = acceptedTags,
                ConfirmedIdentityTags = confirmedIdentityTags,
            });
        }

        var request = new AutoTagReviewRequest
        {
            LibraryId = libraryId,
            Decisions = decisions,
        };
        BeginSmartTagProgress("正在应用审核结果…");
        var job = await RunBackendSubmissionAsync(
            "正在应用自动标签审核结果…",
            "auto_tag_review",
            async backend => await backend.ApiClient.SubmitAutoTagReviewAsync(request)
        );
        if (job is null)
        {
            ResetSmartTagProgress("审核结果未提交");
            ShowValidation("提交审核需要常驻后端，请先确认原生 Python 后端可用。");
            return;
        }
        if (!HandleBackendJob(job, "自动标签审核结果已应用"))
        {
            ResetSmartTagProgress("审核结果未提交");
            return;
        }

        try
        {
            var result = await job.DeserializeResultAsync<AutoTagReviewResult>();
            var refreshed = await LoadAutoTagReviewPageAsync(
                libraryId,
                _autoTagReviewOffset,
                preferPreviousIfEmpty: true
            );
            if (refreshed)
            {
                var pageSummary = AutoTagReviewSummaryTextBlock.Text;
                AutoTagReviewSummaryTextBlock.Text =
                    $"本次接受 {result.Accepted} 项、拒绝 {result.Rejected} 项，" +
                    $"更新 {result.Updated} 张图片；{pageSummary}";
                CompleteSmartTagProgress("审核结果已写入；待审核列表已刷新");
            }
            else
            {
                CompleteSmartTagProgress("审核结果已写入；待审核列表刷新失败");
                AutoTagReviewSummaryTextBlock.Text =
                    $"本次接受 {result.Accepted} 项、拒绝 {result.Rejected} 项，" +
                    $"更新 {result.Updated} 张图片；请点击“恢复 / 刷新”重新加载。";
            }
        }
        catch (Exception exception)
        {
            ShowOperationError("无法读取审核结果", exception);
        }
    }

    private async Task<bool> LoadAutoTagReviewPageAsync(
        string libraryId,
        int requestedOffset,
        bool preferPreviousIfEmpty)
    {
        var normalizedOffset = Math.Max(0, requestedOffset);
        normalizedOffset -= normalizedOffset % AutoTagReviewPageSize;
        BeginSmartTagProgress("正在加载待审核建议…");
        var request = new AutoTagPendingRequest
        {
            LibraryId = libraryId,
            Offset = normalizedOffset,
            Limit = AutoTagReviewPageSize,
            Filters = ReadAutoTagReviewFilters(),
        };
        var job = await RunBackendSubmissionAsync(
            "正在加载待审核建议…",
            "auto_tag_pending",
            async backend => await backend.ApiClient.GetPendingAutoTagsAsync(request)
        );
        if (job is null)
        {
            ResetSmartTagProgress("待审核建议加载失败");
            ShowValidation("恢复待审核建议需要常驻后端，请先确认原生 Python 后端可用。");
            return false;
        }
        if (!HandleBackendJob(job, "待审核建议已加载"))
        {
            ResetSmartTagProgress("待审核建议加载失败");
            return false;
        }

        try
        {
            var page = await PrepareAutoTagReviewPageAsync(job);
            if (
                preferPreviousIfEmpty &&
                page.Result.PendingCount > 0 &&
                page.Items.Count == 0 &&
                page.Result.Offset > 0
            )
            {
                var lastOffset =
                    (page.Result.PendingCount - 1) /
                    AutoTagReviewPageSize *
                    AutoTagReviewPageSize;
                if (lastOffset != page.Result.Offset)
                {
                    return await LoadAutoTagReviewPageAsync(
                        libraryId,
                        lastOffset,
                        preferPreviousIfEmpty: false
                    );
                }
            }

            ApplyAutoTagReviewPage(libraryId, page);
            CompleteSmartTagProgress("待审核建议已刷新");
            return true;
        }
        catch (Exception exception)
        {
            ResetSmartTagProgress("待审核建议加载失败");
            ShowOperationError("无法读取待审核建议", exception);
            return false;
        }
    }

    private static Task<PreparedAutoTagReviewPage> PrepareAutoTagReviewPageAsync(
        BackendJob job) => Task.Run(() =>
        {
            var result = job.DeserializeResult<AutoTagPendingResult>();
            if (result.PendingCount < 0 || result.Offset < 0 || result.Limit <= 0)
            {
                throw new BackendProtocolException(
                    "待审核分页结果包含无效的数量、偏移量或分页大小。"
                );
            }
            if (result.Proposals.Count > result.Limit)
            {
                throw new BackendProtocolException(
                    "待审核分页结果超过了后端声明的分页大小。"
                );
            }

            var items = new List<AutoTagReviewItem>(result.Proposals.Count);
            var skipped = 0;
            foreach (var proposal in result.Proposals)
            {
                if (string.IsNullOrWhiteSpace(proposal.StableId))
                {
                    skipped++;
                    continue;
                }
                items.Add(AutoTagReviewItem.FromProposal(proposal));
            }
            return new PreparedAutoTagReviewPage(result, items, skipped);
        });

    private void ApplyAutoTagReviewPage(
        string libraryId,
        PreparedAutoTagReviewPage page)
    {
        ResetAutoTagThumbnailGeneration();
        _autoTagReviewItems.ReplaceAll(page.Items);

        _autoTagReviewLibraryId = libraryId;
        _autoTagReviewOffset = page.Result.Offset;
        _autoTagReviewPendingCount = page.Result.PendingCount;
        _autoTagReviewHasMore = page.Result.HasMore;
        _autoTagUndoAvailable = page.Result.UndoAvailable;
        if (page.Result.PendingCount == 0)
        {
            AutoTagReviewSummaryTextBlock.Text = "当前图库没有待审核建议。";
        }
        else
        {
            var first = page.Result.Offset + 1;
            var last = page.Result.Offset + page.Items.Count;
            var skipped = page.Skipped == 0
                ? string.Empty
                : $"；跳过 {page.Skipped} 条无有效标识的异常建议";
            AutoTagReviewSummaryTextBlock.Text =
                $"待审核 {page.Result.PendingCount} 项；当前显示 {first}–{last}" +
                $"（每批最多 {AutoTagReviewPageSize} 项）{skipped}" +
                (ReadAutoTagReviewFilters().IsActive ? "；已应用筛选条件。" : "。");
        }
        UpdateAutoTagReviewPageControls();
    }

    private void ResetAutoTagThumbnailGeneration()
    {
        var previous = _autoTagThumbnailCancellation;
        _autoTagThumbnailCancellation = new CancellationTokenSource();
        previous.Cancel();
        previous.Dispose();
    }

    private void UpdateAutoTagReviewPageControls()
    {
        PreviousAutoTagReviewPageButton.IsEnabled = _autoTagReviewOffset > 0;
        NextAutoTagReviewPageButton.IsEnabled = _autoTagReviewHasMore;
        AcceptSelectedLowRiskButton.IsEnabled = _autoTagReviewItems.Any(item =>
            item.IsBatchSelected && item.CanBatchAccept
        );
        var selectedLowRiskCount = _autoTagReviewItems.Count(item =>
            item.IsBatchSelected && item.CanBatchAccept
        );
        AutoTagBatchSelectionTextBlock.Text = $"已选 {selectedLowRiskCount} 张";
        UndoAutoTagBatchButton.IsEnabled = _autoTagUndoAvailable;
        if (_autoTagReviewPendingCount <= 0)
        {
            AutoTagReviewPageTextBlock.Text = "第 0 / 0 批";
            return;
        }
        var currentPage = _autoTagReviewOffset / AutoTagReviewPageSize + 1;
        var pageCount =
            (_autoTagReviewPendingCount + AutoTagReviewPageSize - 1) /
            AutoTagReviewPageSize;
        AutoTagReviewPageTextBlock.Text = $"第 {currentPage} / {pageCount} 批";
    }

    private AutoTagReviewFilters ReadAutoTagReviewFilters() => new()
    {
        LatestIndexOnly = LatestIndexOnlyCheckBox.IsChecked == true,
        Character = AutoTagCharacterFilterTextBox.Text.Trim(),
        Work = AutoTagWorkFilterTextBox.Text.Trim(),
        Action = AutoTagActionFilterTextBox.Text.Trim(),
        Expression = AutoTagExpressionFilterTextBox.Text.Trim(),
        ReviewState = GetSelectedComboValue(AutoTagReviewStateComboBox, "all"),
    };

    private bool TryReadAutoTagOptions(
        bool requireExternalConfirmation,
        out SmartTagOptions options)
    {
        options = null!;
        if (!TryGetSmartTagLibrary(out var libraryId))
        {
            return false;
        }
        if (!TryReadAutoTagExecutionOptions(
            requireExternalConfirmation,
            out var execution
        ))
        {
            return false;
        }

        options = new SmartTagOptions(
            libraryId,
            GetSelectedComboValue(SmartTagScopeComboBox, AutoTagScopes.Untagged),
            execution.Model,
            execution.MaxImages,
            execution.MaxBudgetCny
        );
        return true;
    }

    private bool TryReadAutoTagExecutionOptions(
        bool requireExternalConfirmation,
        out AutoTagExecutionOptions options)
    {
        options = null!;
        if (_modelConfigurationRestartPending)
        {
            ShowValidation(
                "新的模型配置正在等待安全应用。请等待当前任务结束和常驻后端自动重启后再提交标注任务。"
            );
            return false;
        }
        if (!TryParseInteger(SmartTagMaxImagesTextBox.Text, 1, 10_000, out var maxImages))
        {
            ShowValidation("最多处理图片必须是 1 到 10,000 之间的整数。");
            return false;
        }
        if (!TryParsePositiveDecimal(SmartTagBudgetTextBox.Text, out var maxBudgetCny))
        {
            ShowValidation("费用上限必须是大于 0 的数字。");
            return false;
        }
        if (requireExternalConfirmation && ExternalProcessingConfirmCheckBox.IsChecked != true)
        {
            ShowValidation("开始自动标注前，请确认图片会发送到第三方视觉模型处理。");
            return false;
        }

        options = new AutoTagExecutionOptions(
            GetSelectedComboValue(SmartTagModelComboBox, AutoTagModels.Flash),
            maxImages,
            maxBudgetCny
        );
        return true;
    }

    private string DescribeAutoTagModelPolicy(string model)
    {
        if (string.Equals(model, AutoTagModels.Plus, StringComparison.OrdinalIgnoreCase))
        {
            return " Plus（手动全程使用）";
        }
        if (string.Equals(model, AutoTagModels.Flash, StringComparison.OrdinalIgnoreCase))
        {
            return " Flash（仅在冲突或低置信时自动升级 Plus）";
        }
        var selectedName = _activeModelConfiguration.Models?
            .FirstOrDefault(item => string.Equals(item.Id, model, StringComparison.Ordinal))?
            .DisplayName ?? model;
        var escalationId = _activeModelConfiguration.Roles?.AutoTagEscalation;
        var escalationName = _activeModelConfiguration.Models?
            .FirstOrDefault(item => string.Equals(
                item.Id,
                escalationId,
                StringComparison.Ordinal
            ))?.DisplayName ?? escalationId ?? "升级模型";
        return string.Equals(
            model,
            _activeModelConfiguration.Roles?.AutoTagPrimary,
            StringComparison.Ordinal
        )
            ? $" {selectedName}（冲突或低置信时自动升级 {escalationName}）"
            : $" {selectedName}（手动全程使用）";
    }

    private bool ConfirmAutoTagCost(
        string model,
        int maxImages,
        decimal maxBudgetCny,
        string actionPrefix,
        string title)
    {
        var answer = MessageBox.Show(
            this,
            $"{actionPrefix} {maxImages} 张图片发送给" +
            $"{DescribeAutoTagModelPolicy(model)}，" +
            $"费用硬上限为 ¥{maxBudgetCny:0.00}。达到上限后任务会停止。是否继续？",
            title,
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning
        );
        return answer == MessageBoxResult.Yes;
    }

    private bool TryGetSmartTagLibrary(out string libraryId)
    {
        libraryId = string.Empty;
        if (_config is null ||
            SmartTagLibraryComboBox.SelectedItem is not LibraryChoiceItem selected)
        {
            ShowValidation("请选择需要智能整理的图库。");
            return false;
        }

        var library = _config.GetLibrary(selected.LibraryId);
        if (library?.Enabled != true)
        {
            ShowValidation("所选图库已停用，请刷新配置后重试。");
            return false;
        }
        libraryId = library.Id;
        return true;
    }

    private bool TryGetLoadedAutoTagReviewLibrary(out string libraryId)
    {
        if (!TryGetSmartTagLibrary(out libraryId))
        {
            return false;
        }
        if (!string.Equals(libraryId, _autoTagReviewLibraryId, StringComparison.OrdinalIgnoreCase))
        {
            ShowValidation("当前待审核列表属于另一个图库，请先点击“应用筛选”或“恢复 / 刷新”。");
            return false;
        }
        return true;
    }

    private void ShowAutoTagEstimate(AutoTagEstimateResult result)
    {
        _folderTagPreviews.Clear();
        foreach (var preview in result.FolderTagPreview)
        {
            _folderTagPreviews.Add(preview);
        }

        var uncached = Math.Max(0, result.CandidateCount - result.CachedCount);
        SmartTagEstimateSummaryTextBlock.Text =
            $"候选 {result.CandidateCount} 张，缓存命中 {result.CachedCount} 张，" +
            $"预计新增调用 {Math.Max(result.ApiRequestCount, uncached)} 次，" +
            $"预计费用 ¥{result.EstimatedCostCny:0.0000}。";
        CompleteSmartTagProgress("预览完成；尚未向视觉模型发送图片");
    }

    private void ShowAutoTagRun(AutoTagRunSummaryResult result)
    {
        var stop = string.IsNullOrWhiteSpace(result.StoppedReason)
            ? string.Empty
            : $"；停止原因：{result.StoppedReason}";
        var summary =
            $"处理 {result.Processed} 张，成功 {result.Succeeded}，失败 {result.Failed}，" +
            $"缓存命中 {result.Cached}，实际费用 ¥{result.ActualCostCny:0.0000}" +
            $"{stop}。";
        AppendLog($"自动标注完成：{summary}", isError: false);
        AutoTagReviewSummaryTextBlock.Text = summary + " 正在加载首批待审核建议…";
        CompleteSmartTagProgress("建议生成完成，正在加载待审核建议");
    }

    private void BeginSmartTagProgress(string text)
    {
        SmartTagProgressBar.IsIndeterminate = true;
        SmartTagProgressBar.Minimum = 0;
        SmartTagProgressBar.Maximum = 100;
        SmartTagProgressBar.Value = 0;
        SmartTagProgressTextBlock.Text = text;
    }

    private void CompleteSmartTagProgress(string text)
    {
        SmartTagProgressBar.IsIndeterminate = false;
        SmartTagProgressBar.Minimum = 0;
        SmartTagProgressBar.Maximum = 100;
        SmartTagProgressBar.Value = 100;
        SmartTagProgressTextBlock.Text = text;
    }

    private void ResetSmartTagProgress(string text)
    {
        SmartTagProgressBar.IsIndeterminate = false;
        SmartTagProgressBar.Minimum = 0;
        SmartTagProgressBar.Maximum = 100;
        SmartTagProgressBar.Value = 0;
        SmartTagProgressTextBlock.Text = text;
    }

    private void UpdateSmartTagProgress(BackendJob job)
    {
        if (!SmartTagCommands.Contains(job.Command))
        {
            return;
        }

        var progress = job.Progress;
        SmartTagProgressTextBlock.Text = progress?.Message ?? job.Status switch
        {
            "queued" => "任务正在排队…",
            "paused" => "任务已暂停",
            "needs_attention" => "任务需要处理，请查看“状态与诊断”",
            "cancelling" => "正在取消任务…",
            _ => $"任务状态：{job.Status}",
        };
        if (progress?.Current is int current && progress.Total is > 0)
        {
            SmartTagProgressBar.IsIndeterminate = false;
            SmartTagProgressBar.Minimum = 0;
            SmartTagProgressBar.Maximum = progress.Total.Value;
            SmartTagProgressBar.Value = Math.Min(current, progress.Total.Value);
        }
        else
        {
            SmartTagProgressBar.IsIndeterminate =
                !job.IsTerminal && !job.IsPaused && !job.NeedsAttention;
        }
    }

    private static bool TryParsePositiveDecimal(string text, out decimal value) =>
        (decimal.TryParse(text, NumberStyles.Number, CultureInfo.CurrentCulture, out value) ||
         decimal.TryParse(text, NumberStyles.Number, CultureInfo.InvariantCulture, out value)) &&
        value > 0;

    private sealed record SmartTagOptions(
        string LibraryId,
        string Scope,
        string Model,
        int MaxImages,
        decimal MaxBudgetCny
    );

    private sealed record AutoTagExecutionOptions(
        string Model,
        int MaxImages,
        decimal MaxBudgetCny
    );

    private sealed record PreparedAutoTagReviewPage(
        AutoTagPendingResult Result,
        IReadOnlyList<AutoTagReviewItem> Items,
        int Skipped
    );
}
