using System.Collections.Concurrent;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;
using Zvec.Desktop.Collections;
using Zvec.Desktop.Models;
using Zvec.Desktop.Services;

namespace Zvec.Desktop;

public partial class MainWindow : Window
{
    private const int SearchResultPageSize = 15;

    private enum BackendStatusKind
    {
        Starting,
        Ready,
        Fallback,
    }

    private sealed record EnvironmentPathProbeResult(
        string ImageRoot,
        string Workspace,
        string Results,
        bool ImageRootExists,
        bool ImageRootReadable,
        bool WorkspaceExists,
        bool WorkspaceWritable,
        bool WorkspaceHasIndex,
        bool ResultsExists,
        bool ResultsWritable,
        string? LayoutError
    );

    private static readonly Regex EmbeddingProgressPattern = new(
        @"Embedded\s+(\d+)/(\d+)\s+unique images",
        RegexOptions.Compiled | RegexOptions.CultureInvariant | RegexOptions.IgnoreCase
    );

    private readonly LauncherConfigService _configService = new();
    private readonly ApiCredentialService _apiCredentialService = new();
    private readonly SearchResultLoader _resultLoader = new();
    private readonly RangeObservableCollection<SearchResultItem> _results = [];
    private readonly PagedCollectionState<SearchResultItem> _resultPaging =
        new(SearchResultPageSize);
    private readonly ObservableCollection<LibraryListItem> _libraryItems = [];
    private readonly ObservableCollection<LibraryChoiceItem> _indexLibraryChoices = [];
    private readonly ObservableCollection<LibrarySearchScopeItem> _searchLibraryScopes = [];
    private readonly ObservableCollection<BackendTaskItem> _backendTaskItems = [];
    private readonly Dictionary<string, BackendTaskItem> _backendTasks = new(StringComparer.Ordinal);
    private readonly HashSet<string> _terminalBackendTaskIds = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _lastBackendProgressMessages = new(StringComparer.Ordinal);
    private readonly ConcurrentQueue<(string Line, bool IsError)> _pendingLogLines = new();
    private readonly DispatcherTimer _logFlushTimer = new(DispatcherPriority.Background);
    private readonly BackendLifecycleCoordinator _backendLifecycle = new();
    private readonly DesktopClosePolicy _closePolicy = new();
    private PowerShellZvecRunner? _runner;
    private RuntimeBootstrapService? _runtimeBootstrapService;
    private WorkspaceMigrationService? _workspaceMigrationService;
    private RuntimeBootstrapResult? _runtimeBootstrapResult;
    private string? _runtimeBootstrapPythonSelection;
    private bool _runtimeSelectionDirty;
    private CommandResult? _lastEnvironmentDoctorResult;
    private CancellationTokenSource? _environmentOperationCancellation;
    private CancellationTokenSource? _environmentPathProbeCancellation;
    private readonly CancellationTokenSource _backendObservationCancellation = new();
    private CancellationTokenSource? _backendJobMonitorCancellation;
    private Task? _backendJobMonitorTask;
    private BackendHostService? _backendJobMonitorHost;
    private bool _drainBackendRestartScheduled;
    private EnvironmentPathProbeResult? _environmentPathProbe;
    private BackendHostService? _backendHost;
    private LauncherConfig? _config;
    private LauncherConfigInspection? _configInspection;
    private string? _repositoryRoot;
    private bool _isBusy;
    private int _pendingLogLineCount;
    private int _droppedLogLineCount;
    private int _renderedLogCharacterCount;
    private RunnerProgressUpdate? _pendingRunnerProgress;
    private int _runnerProgressDispatchScheduled;
    private bool _allowClose;
    private bool _isClosing;
    private bool _hasStoredApiKey;
    private bool _refreshingLibrarySelection;
    private bool _supportsLowConfidenceOverride;
    private SearchResultSession? _currentResultSession;
    private string? _startupError;
    private string? _workspaceMigrationStatusOverride;
    private LauncherLibrary? _libraryDraft;

    public event EventHandler? HiddenToTray;

    public MainWindow()
    {
        InitializeComponent();
        _logFlushTimer.Interval = TimeSpan.FromMilliseconds(250);
        _logFlushTimer.Tick += (_, _) => FlushPendingLogLines();
        _logFlushTimer.Start();
        try
        {
            _repositoryRoot = RepositoryLocator.FindRepositoryRoot();
            _runner = new PowerShellZvecRunner(_repositoryRoot);
            _runner.OutputReceived += Runner_OutputReceived;
            _runtimeBootstrapService = new RuntimeBootstrapService(_runner);
            _workspaceMigrationService = new WorkspaceMigrationService(_runner);
        }
        catch (Exception exception)
        {
            _startupError = exception.Message;
        }
    }

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        CancelButton.IsEnabled = false;
        BackendTaskList.ItemsSource = _backendTaskItems;
        ResultsList.ItemsSource = _results;
        LibraryListBox.ItemsSource = _libraryItems;
        IndexLibraryComboBox.ItemsSource = _indexLibraryChoices;
        SearchLibraryScopeComboBox.ItemsSource = _searchLibraryScopes;
        SearchModeComboBox.SelectedIndex = Math.Max(0, SearchModeComboBox.SelectedIndex);
        TagModeComboBox.SelectedIndex = Math.Max(0, TagModeComboBox.SelectedIndex);
        InitializeQueryImageInput();
        UpdateSearchModePresentation();
        await ReloadConfigurationAsync(loadLatestResults: true);
        // Materialize and validate models.json before the persistent backend starts,
        // so both processes observe the same first-use defaults.
        await ReloadModelConfigurationAsync(announceInLog: false);
        await RefreshEnvironmentStatusAsync(runDoctor: false);

        if (_config is not null && _runtimeBootstrapResult?.IsSuccess == true)
        {
            _ = TryStartBackendAsync(showFailure: false);
        }
        else if (_config is not null &&
            IsAutomaticallyRepairable(_runtimeBootstrapResult))
        {
            await RepairEnvironmentAsync();
        }

        if (!string.IsNullOrWhiteSpace(_startupError))
        {
            StatusTextBlock.Text = "桌面程序未找到项目脚本";
            MessageBox.Show(
                this,
                _startupError,
                "Zvec 图片搜索",
                MessageBoxButton.OK,
                MessageBoxImage.Error
            );
        }
    }

    private async void Window_Closing(object? sender, CancelEventArgs e)
    {
        var closeAction = _closePolicy.Resolve(_allowClose);
        if (closeAction == DesktopCloseAction.CompleteApplicationExit)
        {
            _logFlushTimer.Stop();
            FlushPendingLogLines();
            DisposeQueryImageInput();
            CancelSelectedResultPreview();
            _environmentOperationCancellation?.Dispose();
            _environmentPathProbeCancellation?.Cancel();
            _backendObservationCancellation.Dispose();
            _backendJobMonitorCancellation?.Dispose();
            _runtimeBootstrapService?.Dispose();
            _runner?.Dispose();
            _backendLifecycle.Dispose();
            return;
        }

        e.Cancel = true;
        if (closeAction == DesktopCloseAction.HideToTray)
        {
            Hide();
            HiddenToTray?.Invoke(this, EventArgs.Empty);
            return;
        }
        if (_isClosing)
        {
            return;
        }

        var preserveBackendTasks = HasActiveBackendTasks;
        if (_isBusy || preserveBackendTasks)
        {
            var answer = MessageBox.Show(
                this,
                preserveBackendTasks
                    ? "后台任务仍在运行。是否让任务继续在后台运行并退出？下次打开软件时会恢复到“状态与诊断”的后台任务列表。"
                    : "当前本地操作仍在运行，退出会停止该操作。是否继续退出？",
                "确认退出",
                MessageBoxButton.YesNo,
                MessageBoxImage.Warning
            );
            if (answer != MessageBoxResult.Yes)
            {
                _closePolicy.CancelExit();
                return;
            }
        }

        _isClosing = true;
        _backendLifecycle.RequestShutdown();
        _backendObservationCancellation.Cancel();
        try
        {
            // Closing the window stops only local observation. Backend jobs are
            // deliberately not cancelled, so a later window can recover them.
            await StopBackendJobMonitorAsync();
            if (_isBusy)
            {
                await CancelCurrentOperationAsync();
                await WaitForCurrentOperationAsync(TimeSpan.FromSeconds(8));
            }
            if (preserveBackendTasks && _backendHost is not null)
            {
                await DetachBackendAsync();
            }
            else
            {
                await StopBackendAsync();
            }
        }
        catch (Exception exception)
        {
            AppendLog($"关闭后台服务时发生错误：{exception.Message}", isError: true);
        }
        finally
        {
            _allowClose = true;
            // StopBackendAsync can complete synchronously when no persistent
            // backend exists. Calling Close() re-entrantly from the original
            // Closing event is ignored by WPF while that event still has
            // Cancel=true, leaving the window open indefinitely. Queue the
            // second close so it runs after the current Closing event returns.
            _ = Dispatcher.BeginInvoke(new Action(Close));
        }
    }

    /// <summary>
    /// Requests the full shutdown path. Ordinary window-close gestures do not
    /// call this method and therefore keep backend work alive in the tray.
    /// </summary>
    public void RequestApplicationExit()
    {
        if (_allowClose || _isClosing)
        {
            return;
        }
        _closePolicy.RequestExit();
        Close();
    }

    private void ExitApplication_Click(object sender, RoutedEventArgs e) =>
        RequestApplicationExit();

    private void BrowseImageRoot_Click(object sender, RoutedEventArgs e) =>
        BrowseFolder(SetupImageRootTextBox, "选择需要索引的图片目录");

    private void BrowseWorkspace_Click(object sender, RoutedEventArgs e)
        => BrowseFolder(SetupWorkspaceTextBox, "选择 Collection workspace 目录");

    private void BrowseResults_Click(object sender, RoutedEventArgs e) =>
        BrowseFolder(SetupResultsTextBox, "选择搜索结果目录");

    private void BrowseQueryImage_Click(object sender, RoutedEventArgs e)
        => ChooseQueryImageFromFile();

    private void LibraryList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_refreshingLibrarySelection ||
            LibraryListBox.SelectedItem is not LibraryListItem item ||
            _config?.GetLibrary(item.Id) is not LauncherLibrary library)
        {
            return;
        }

        BeginEditLibrary(library);
    }

    private void AddLibrary_Click(object sender, RoutedEventArgs e)
    {
        var template = _configService.CreateLibraryTemplate();
        _refreshingLibrarySelection = true;
        LibraryListBox.SelectedItem = null;
        _refreshingLibrarySelection = false;
        BeginEditLibrary(template, isNew: true);
    }

    private void EditLibrary_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetSelectedConfiguredLibrary(out var selected))
        {
            ShowValidation("请先从图库列表选择一项。");
            return;
        }

        BeginEditLibrary(selected);
        SetupLibraryNameTextBox.Focus();
        SetupLibraryNameTextBox.SelectAll();
    }

    private async void ToggleLibrary_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetSelectedConfiguredLibrary(out var selected) || _config is null)
        {
            ShowValidation("请先从图库列表选择一项。");
            return;
        }

        var next = CopyConfig(_config);
        var target = next.GetLibrary(selected.Id)!;
        if (target.Enabled)
        {
            var replacement = next.Libraries.FirstOrDefault(
                library => library.Enabled &&
                    !string.Equals(library.Id, target.Id, StringComparison.OrdinalIgnoreCase)
            );
            if (replacement is null)
            {
                ShowValidation("至少需要保留一个已启用图库。");
                return;
            }
            target.Enabled = false;
            if (string.Equals(
                next.DefaultLibraryId,
                target.Id,
                StringComparison.OrdinalIgnoreCase
            ))
            {
                next.DefaultLibraryId = replacement.Id;
            }
        }
        else
        {
            target.Enabled = true;
        }

        await ApplyConfigurationChangeAsync(
            next,
            target.Enabled ? $"图库“{target.Name}”已启用" : $"图库“{target.Name}”已停用",
            target.Id
        );
    }

    private async void SetDefaultLibrary_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetSelectedConfiguredLibrary(out var selected) || _config is null)
        {
            ShowValidation("请先从图库列表选择一项。");
            return;
        }
        if (!selected.Enabled)
        {
            ShowValidation("默认图库必须处于启用状态。");
            return;
        }
        if (string.Equals(
            _config.DefaultLibraryId,
            selected.Id,
            StringComparison.OrdinalIgnoreCase
        ))
        {
            StatusTextBlock.Text = $"“{selected.Name}”已经是默认图库";
            return;
        }

        var next = CopyConfig(_config);
        next.DefaultLibraryId = selected.Id;
        await ApplyConfigurationChangeAsync(
            next,
            $"默认图库已切换为“{selected.Name}”",
            selected.Id
        );
    }

    private async void DeleteLibrary_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetSelectedConfiguredLibrary(out var selected) || _config is null)
        {
            ShowValidation("请先从图库列表选择一项。");
            return;
        }
        if (_config.Libraries.Count <= 1)
        {
            ShowValidation("至少需要保留一个图库配置。");
            return;
        }

        var answer = MessageBox.Show(
            this,
            $"确定从应用中移除图库“{selected.Name}”吗？\n\n" +
                "此操作只删除配置，不会删除图片、Workspace 或 Collection。",
            "移除图库",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning
        );
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }

        var next = CopyConfig(_config);
        next.Libraries.RemoveAll(library => string.Equals(
            library.Id,
            selected.Id,
            StringComparison.OrdinalIgnoreCase
        ));
        if (string.Equals(
            next.DefaultLibraryId,
            selected.Id,
            StringComparison.OrdinalIgnoreCase
        ))
        {
            next.DefaultLibraryId = next.Libraries.FirstOrDefault(library => library.Enabled)?.Id ??
                next.Libraries[0].Id;
            next.GetLibrary(next.DefaultLibraryId)!.Enabled = true;
        }

        await ApplyConfigurationChangeAsync(
            next,
            $"图库“{selected.Name}”已从配置移除，数据保持不变",
            next.DefaultLibraryId
        );
    }

    private async void Initialize_Click(object sender, RoutedEventArgs e)
    {
        if (_configInspection?.RequiresMigration == true)
        {
            ShowValidation("检测到旧版图库配置，请先使用上方“备份与迁移”完成升级。");
            return;
        }
        if (!EnsureRunner())
        {
            return;
        }

        if (_config is not null)
        {
            await SaveLibraryDraftAsync();
            return;
        }

        var libraryName = SetupLibraryNameTextBox.Text.Trim();
        var imageRoot = SetupImageRootTextBox.Text.Trim();
        var workspace = SetupWorkspaceTextBox.Text.Trim();
        var results = SetupResultsTextBox.Text.Trim();
        var pythonExecutable = SetupPythonExecutableTextBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(libraryName))
        {
            ShowValidation("图库名称不能为空。");
            return;
        }
        if (!Directory.Exists(imageRoot))
        {
            ShowValidation("请选择存在的图片目录。");
            return;
        }
        if (string.IsNullOrWhiteSpace(workspace) || string.IsNullOrWhiteSpace(results))
        {
            ShowValidation("Workspace 和搜索结果目录不能为空。");
            return;
        }
        if (!TryValidateEnvironmentPathsForSave(out var pathValidationError))
        {
            ShowValidation(pathValidationError);
            return;
        }
        bool hasApiKey;
        try
        {
            hasApiKey = _apiCredentialService.HasApiKey();
        }
        catch (Exception exception)
        {
            ShowOperationError("无法读取 API Key", exception);
            return;
        }
        if (string.IsNullOrWhiteSpace(ApiKeyPasswordBox.Password) && !hasApiKey)
        {
            ShowValidation("首次初始化需要输入 DASHSCOPE_API_KEY。");
            return;
        }

        try
        {
            Directory.CreateDirectory(workspace);
            Directory.CreateDirectory(results);
            if (!string.IsNullOrWhiteSpace(ApiKeyPasswordBox.Password))
            {
                _apiCredentialService.SaveApiKey(ApiKeyPasswordBox.Password);
                _hasStoredApiKey = true;
            }
        }
        catch (Exception exception)
        {
            ShowOperationError("无法准备目录或保存 API Key", exception);
            return;
        }

        SetBusy(true, "正在初始化首个图库 Collection…");
        try
        {
            await StopBackendAsync();
            StatusTextBlock.Text = "正在初始化首个图库 Collection…";
            var arguments = new List<string>
            {
                imageRoot,
                "--workspace",
                workspace,
                "--results",
                results,
                "--skip-key",
            };

            var result = await _runner!.RunZvecAsync(
                "init",
                arguments,
                FirstUseInitializationEnvironment.Create(pythonExecutable)
            );
            if (HandleCommandResult(result, "初始化完成", allowPartial: false))
            {
                ApiKeyPasswordBox.Clear();
                await ReloadConfigurationAsync(loadLatestResults: false);
                if (_config?.DefaultLibrary is LauncherLibrary initializedLibrary &&
                    (!string.Equals(
                        initializedLibrary.Name,
                        libraryName,
                        StringComparison.CurrentCulture
                    ) ||
                    !string.Equals(
                        _config.PythonExecutable,
                        pythonExecutable,
                        StringComparison.OrdinalIgnoreCase
                    )))
                {
                    var renamed = CopyConfig(_config);
                    renamed.GetLibrary(initializedLibrary.Id)!.Name = libraryName;
                    renamed.PythonExecutable = string.IsNullOrWhiteSpace(pythonExecutable)
                        ? null
                        : pythonExecutable;
                    await _configService.SaveAsync(renamed);
                    _config = renamed;
                    RefreshLibraryControls(initializedLibrary.Id);
                }
                _ = TryStartBackendAsync(showFailure: false);
            }
        }
        catch (Exception exception)
        {
            ShowOperationError("初始化失败", exception);
        }
        finally
        {
            SetBusy(false, StatusTextBlock.Text);
        }
    }

    private async void Doctor_Click(object sender, RoutedEventArgs e)
    {
        await RefreshEnvironmentStatusAsync(runDoctor: true);
    }

    private async void RefreshEnvironment_Click(object sender, RoutedEventArgs e)
    {
        await RefreshEnvironmentStatusAsync(runDoctor: _config is not null);
    }

    private async void RepairEnvironment_Click(object sender, RoutedEventArgs e)
    {
        await RepairEnvironmentAsync();
    }

    private async Task RepairEnvironmentAsync()
    {
        if (!EnsureNoActiveBackendTasks("修复运行环境"))
        {
            return;
        }
        if (_runtimeBootstrapService is null)
        {
            ShowValidation(_startupError ?? "运行环境修复服务不可用，请重新安装桌面应用。");
            return;
        }

        var restartBackend = _backendHost?.IsRunning == true;
        BeginEnvironmentOperation("正在自动准备隔离运行环境…");
        try
        {
            if (restartBackend)
            {
                StatusTextBlock.Text = "正在安全停止常驻后端…";
                AppendLog("修复运行环境前先停止常驻后端，避免覆盖正在使用的文件。", false);
                await StopBackendAsync();
            }
            StatusTextBlock.Text = "正在自动准备隔离运行环境…";
            var pythonSelection = NormalizeOptionalPath(
                SetupPythonExecutableTextBox.Text
            );
            _runtimeBootstrapResult = await _runtimeBootstrapService.EnsureReadyAsync(
                pythonSelection,
                _environmentOperationCancellation!.Token
            );
            if (!string.Equals(
                _runtimeBootstrapResult.Status,
                "cancelled",
                StringComparison.OrdinalIgnoreCase
            ))
            {
                _runtimeBootstrapPythonSelection = pythonSelection;
                _runtimeSelectionDirty = false;
            }
            AppendRuntimeBootstrapLog(_runtimeBootstrapResult);
            DoctorOutputTextBox.Text = BuildRuntimeDiagnosticText(_runtimeBootstrapResult);
            if (string.Equals(
                _runtimeBootstrapResult.Status,
                "cancelled",
                StringComparison.OrdinalIgnoreCase
            ))
            {
                StatusTextBlock.Text = "运行环境修复已取消";
                return;
            }
            if (!_runtimeBootstrapResult.IsSuccess)
            {
                StatusTextBlock.Text = "运行环境修复未完成";
                return;
            }

            await RefreshEnvironmentPathProbeAsync(
                TimeSpan.Zero,
                _environmentOperationCancellation.Token
            );

            StatusTextBlock.Text = _runtimeBootstrapResult.Changed
                ? "运行环境已自动修复"
                : "运行环境已经就绪";
            if (_config is not null)
            {
                await RunDoctorWithoutBusyTransitionAsync();
            }
        }
        catch (OperationCanceledException)
        {
            StatusTextBlock.Text = "运行环境修复已取消";
        }
        catch (Exception exception)
        {
            ShowOperationError("自动修复运行环境失败", exception);
        }
        finally
        {
            EndEnvironmentOperation();
            if (restartBackend && _config is not null && _backendHost?.IsRunning != true)
            {
                await TryStartBackendAsync(showFailure: false);
            }
            ApplyEnvironmentSnapshot();
        }
    }

    private void OpenEnvironmentLog_Click(object sender, RoutedEventArgs e)
    {
        MainTabControl.SelectedItem = StatusDiagnosticsTabItem;
        TaskLogExpander.IsExpanded = true;
        RuntimeLogExpander.IsExpanded = true;
        _ = Dispatcher.BeginInvoke(
            new Action(() =>
            {
                FlushPendingLogLines();
                LogTextBox.Focus();
                LogTextBox.CaretIndex = LogTextBox.Text.Length;
                LogTextBox.ScrollToEnd();
            }),
            DispatcherPriority.ContextIdle
        );
    }

    private async void BackupMigration_Click(object sender, RoutedEventArgs e)
    {
        if (!EnsureNoActiveBackendTasks("迁移 Workspace"))
        {
            return;
        }
        if (_workspaceMigrationService is null)
        {
            ShowValidation("Workspace 备份与迁移服务不可用，请重新安装桌面应用。");
            return;
        }

        var isLegacyMigration = _configInspection?.RequiresMigration == true;
        LauncherLibrary? library = null;
        if (!isLegacyMigration)
        {
            if (!TryGetSelectedConfiguredLibrary(out var selectedLibrary))
            {
                ShowValidation("请先选择包含现有索引的图库。");
                return;
            }
            library = selectedLibrary;
        }
        var libraryName = isLegacyMigration
            ? _configInspection?.LibraryName ?? "旧版图库配置"
            : library!.Name;
        var libraryId = isLegacyMigration ? null : library!.Id;

        var restartBackend = _backendHost?.IsRunning == true;
        var migrationSucceeded = false;
        _workspaceMigrationStatusOverride = null;
        BeginEnvironmentOperation("正在预检 Workspace 备份与迁移…");
        try
        {
            if (restartBackend)
            {
                AppendLog("备份前先停止常驻后端，以安全释放 Workspace 文件锁。", false);
                await StopBackendAsync();
            }

            var request = new WorkspaceMigrationRequest(LibraryId: libraryId);
            var planResponse = await _workspaceMigrationService.PlanMigrationAsync(
                request,
                _environmentOperationCancellation!.Token
            );
            EnsureZeroApiMigration(planResponse.Report.ApiRequests, "迁移预检");
            var backupPlan = planResponse.Report.EffectiveWorkspaceBackup?.Plan;
            if (backupPlan?.Blockers.Count > 0)
            {
                _workspaceMigrationStatusOverride =
                    $"Workspace：暂时不能迁移 · {backupPlan.Blockers[0]}";
                ShowValidation(string.Join(Environment.NewLine, backupPlan.Blockers));
                return;
            }
            if (!planResponse.IsSuccess)
            {
                throw new InvalidOperationException(
                    LastUsefulLine(planResponse.Command.StandardError) ??
                    LastUsefulLine(planResponse.Command.StandardOutput) ??
                    "Workspace 迁移预检失败。"
                );
            }

            var planDetail = WorkspaceMigrationConfirmationFormatter.Build(
                backupPlan,
                _configInspection?.NamedVolumes
            );
            var answer = MessageBox.Show(
                this,
                $"即将处理“{libraryName}”。{Environment.NewLine}{Environment.NewLine}" +
                $"{planDetail}{Environment.NewLine}{Environment.NewLine}" +
                (_configInspection?.HasNamedVolume == true
                    ? "旧版 named volume 将一次性通过 Docker 导出；之后日常使用不再需要 Docker。" +
                        Environment.NewLine
                    : string.Empty) +
                "此过程复用已有向量，不会调用模型 API。是否继续？",
                "备份并迁移 Workspace",
                MessageBoxButton.YesNo,
                MessageBoxImage.Question
            );
            if (answer != MessageBoxResult.Yes)
            {
                _workspaceMigrationStatusOverride =
                    "Workspace：预检完成，尚未执行任何修改。";
                StatusTextBlock.Text = "已取消备份与迁移";
                return;
            }

            StatusTextBlock.Text = "正在备份并迁移 Workspace…";
            var migrationResponse = await _workspaceMigrationService.MigrateAsync(
                request,
                _environmentOperationCancellation.Token
            );
            EnsureZeroApiMigration(migrationResponse.Report.ApiRequests, "Workspace 迁移");
            if (!migrationResponse.IsSuccess)
            {
                throw new InvalidOperationException(
                    LastUsefulLine(migrationResponse.Command.StandardError) ??
                    LastUsefulLine(migrationResponse.Command.StandardOutput) ??
                    "Workspace 备份与迁移失败。"
                );
            }

            var backup = migrationResponse.Report.EffectiveWorkspaceBackup;
            var completedMigrationStatus = backup?.Destination is { Length: > 0 }
                ? $"Workspace：迁移完成 · 备份保存在 {backup.Destination}"
                : "Workspace：迁移与完整性校验已完成，备份已保留。";
            AppendLog(
                $"Workspace 备份与迁移完成；API 请求数={migrationResponse.Report.ApiRequests}。",
                false
            );
            StatusTextBlock.Text = "Workspace 备份与迁移完成";
            await ReloadConfigurationAsync(loadLatestResults: false);
            _workspaceMigrationStatusOverride = completedMigrationStatus;
            migrationSucceeded = true;
        }
        catch (OperationCanceledException)
        {
            StatusTextBlock.Text = "Workspace 备份与迁移已取消";
            _workspaceMigrationStatusOverride =
                "Workspace：操作已取消；已完成的安全备份会保留。";
            AppendLog("Workspace 操作已取消；已完成的安全备份会保留。", false);
        }
        catch (Exception) when (
            _environmentOperationCancellation?.IsCancellationRequested == true
        )
        {
            StatusTextBlock.Text = "Workspace 备份与迁移已取消";
            _workspaceMigrationStatusOverride =
                "Workspace：操作已取消；已完成的安全备份会保留。";
            AppendLog("Workspace 操作已取消；已完成的安全备份会保留。", false);
        }
        catch (Exception exception)
        {
            _workspaceMigrationStatusOverride =
                "Workspace：操作失败；原数据和已完成的备份保持不变。";
            ShowOperationError("Workspace 备份与迁移失败", exception);
        }
        finally
        {
            EndEnvironmentOperation();
            if ((restartBackend || migrationSucceeded) &&
                _config is not null &&
                _backendHost?.IsRunning != true)
            {
                await TryStartBackendAsync(showFailure: false);
            }
            ApplyEnvironmentSnapshot();
        }
    }

    private void EnvironmentPath_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (IsLoaded)
        {
            _workspaceMigrationStatusOverride = null;
            _environmentPathProbe = null;
            ApplyEnvironmentSnapshot();
            QueueEnvironmentPathProbe();
        }
    }

    private void ApiKeyPasswordBox_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (IsLoaded)
        {
            ApplyEnvironmentSnapshot();
        }
    }

    private void PythonExecutable_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (!IsLoaded)
        {
            return;
        }
        var selection = NormalizeOptionalPath(SetupPythonExecutableTextBox.Text);
        _runtimeSelectionDirty = !string.Equals(
            selection,
            _runtimeBootstrapPythonSelection,
            StringComparison.OrdinalIgnoreCase
        );
        if (_runtimeSelectionDirty)
        {
            _lastEnvironmentDoctorResult = null;
        }
        ApplyEnvironmentSnapshot();
    }

    private async Task RefreshEnvironmentStatusAsync(bool runDoctor)
    {
        if (_runtimeBootstrapService is null)
        {
            ApplyEnvironmentSnapshot();
            return;
        }

        BeginEnvironmentOperation("正在检测首次使用环境…");
        try
        {
            if (!runDoctor)
            {
                _lastEnvironmentDoctorResult = null;
            }
            var pythonSelection = NormalizeOptionalPath(
                SetupPythonExecutableTextBox.Text
            );
            _runtimeBootstrapResult = await _runtimeBootstrapService.DiagnoseAsync(
                pythonSelection,
                _environmentOperationCancellation!.Token
            );
            if (!string.Equals(
                _runtimeBootstrapResult.Status,
                "cancelled",
                StringComparison.OrdinalIgnoreCase
            ))
            {
                _runtimeBootstrapPythonSelection = pythonSelection;
                _runtimeSelectionDirty = false;
            }
            await RefreshEnvironmentPathProbeAsync(
                TimeSpan.Zero,
                _environmentOperationCancellation.Token
            );
            DoctorOutputTextBox.Text = BuildRuntimeDiagnosticText(
                _runtimeBootstrapResult
            );
            if (string.Equals(
                _runtimeBootstrapResult.Status,
                "cancelled",
                StringComparison.OrdinalIgnoreCase
            ))
            {
                StatusTextBlock.Text = "环境检测已取消";
                return;
            }
            if (runDoctor && _runtimeBootstrapResult.IsSuccess && _config is not null)
            {
                await RunDoctorWithoutBusyTransitionAsync();
            }

            StatusTextBlock.Text = _runtimeBootstrapResult.IsSuccess
                ? _lastEnvironmentDoctorResult is { IsSuccess: false }
                    ? "运行环境可用，图库配置需要处理"
                    : "环境检测完成"
                : "运行环境需要修复";
        }
        catch (OperationCanceledException)
        {
            StatusTextBlock.Text = "环境检测已取消";
        }
        catch (Exception exception)
        {
            ShowOperationError("环境检测失败", exception);
        }
        finally
        {
            EndEnvironmentOperation();
            ApplyEnvironmentSnapshot();
        }
    }

    private async Task RunDoctorWithoutBusyTransitionAsync()
    {
        if (_runner is null)
        {
            return;
        }

        var environment = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase);
        try
        {
            var apiKey = _apiCredentialService.ReadApiKey();
            if (!string.IsNullOrWhiteSpace(apiKey))
            {
                environment["DASHSCOPE_API_KEY"] = apiKey;
            }
        }
        catch (Exception exception)
        {
            AppendLog($"读取 API Key 状态失败：{exception.Message}", true);
        }

        _lastEnvironmentDoctorResult = await _runner.RunZvecAsync(
            "doctor",
            environment: environment,
            cancellationToken: _environmentOperationCancellation?.Token ??
                CancellationToken.None
        );
        var doctorText = _lastEnvironmentDoctorResult.CombinedOutput.Trim();
        DoctorOutputTextBox.Text = string.IsNullOrWhiteSpace(doctorText)
            ? BuildRuntimeDiagnosticText(_runtimeBootstrapResult)
            : doctorText;
        if (!_lastEnvironmentDoctorResult.IsSuccess)
        {
            StatusTextBlock.Text = "运行环境已就绪，但图库配置检查发现问题";
        }
        if (_lastEnvironmentDoctorResult.IsSuccess &&
            _backendHost?.IsRunning != true)
        {
            await TryStartBackendAsync(showFailure: false);
        }
    }

    private void BeginEnvironmentOperation(string status)
    {
        _environmentPathProbeCancellation?.Cancel();
        _environmentOperationCancellation?.Cancel();
        _environmentOperationCancellation?.Dispose();
        _environmentOperationCancellation = new CancellationTokenSource();
        SetBusy(true, status);
        ApplyEnvironmentSnapshot(isChecking: true);
    }

    private void EndEnvironmentOperation()
    {
        _environmentOperationCancellation?.Dispose();
        _environmentOperationCancellation = null;
        SetBusy(false, StatusTextBlock.Text);
    }

    private void ApplyEnvironmentSnapshot(bool isChecking = false)
    {
        var runtimeReady = !_runtimeSelectionDirty &&
            (_runtimeBootstrapResult?.IsSuccess == true ||
                _backendHost?.IsRunning == true);
        var dependencyList = _runtimeBootstrapResult?.Dependencies ?? [];
        var dependenciesReady = runtimeReady && dependencyList.All(
            dependency => dependency.Imported
        );
        var apiKeyConfigured = _hasStoredApiKey ||
            !string.IsNullOrWhiteSpace(ApiKeyPasswordBox.Password);

        var pythonDetail = _runtimeSelectionDirty
            ? "自定义 Python 已更改，请点击“重新检测”或“修复运行环境”。"
            : runtimeReady
            ? BuildPythonDetail(_runtimeBootstrapResult)
            : _runtimeBootstrapResult?.Message ?? "将自动选择兼容的隔离 Python";
        var dependencyDetail = _runtimeSelectionDirty
            ? "解释器变更后需要重新验证 zvec、Pillow 和 numpy。"
            : dependenciesReady
            ? BuildDependencyDetail(dependencyList)
            : _runtimeBootstrapResult?.RecommendedAction ?? "点击自动修复安装锁定依赖";
        var imageRoot = SetupImageRootTextBox.Text.Trim();
        var workspace = SetupWorkspaceTextBox.Text.Trim();
        var results = SetupResultsTextBox.Text.Trim();
        var pathProbe = _environmentPathProbe is not null &&
            string.Equals(_environmentPathProbe.ImageRoot, imageRoot, StringComparison.Ordinal) &&
            string.Equals(_environmentPathProbe.Workspace, workspace, StringComparison.Ordinal) &&
            string.Equals(_environmentPathProbe.Results, results, StringComparison.Ordinal)
                ? _environmentPathProbe
                : null;
        var snapshot = FirstUseEnvironmentEvaluator.Evaluate(
            new FirstUseEnvironmentInput
            {
                RunnerAvailable = _runner is not null &&
                    _runtimeBootstrapService is not null,
                PythonReady = runtimeReady,
                PythonDetail = pythonDetail,
                DependenciesReady = dependenciesReady,
                DependenciesDetail = dependencyDetail,
                ApiKeyConfigured = apiKeyConfigured,
                ImageRoot = imageRoot,
                ImageRootExists = pathProbe?.ImageRootExists == true,
                ImageRootReadable = pathProbe?.ImageRootReadable == true,
                WorkspaceDirectory = workspace,
                WorkspaceExists = pathProbe?.WorkspaceExists == true,
                WorkspaceWritable = pathProbe?.WorkspaceWritable == true,
                ResultsDirectory = results,
                ResultsDirectoryExists = pathProbe?.ResultsExists == true,
                ResultsDirectoryWritable = pathProbe?.ResultsWritable == true,
                PathLayoutError = pathProbe?.LayoutError,
                PathChecksPending = pathProbe is null,
                IsChecking = isChecking,
            }
        );

        EnvironmentSummaryTextBlock.Text = _backendHost?.IsRunning == true
            ? "运行环境与常驻后端已就绪"
            : snapshot.Summary;
        var runtimeFailureAction = !isChecking &&
            _runtimeBootstrapResult is { IsSuccess: false } failed &&
            !string.IsNullOrWhiteSpace(failed.RecommendedAction)
                ? $"{snapshot.NextAction} {failed.RecommendedAction}"
                : null;
        var doctorFailure = !isChecking &&
            _lastEnvironmentDoctorResult is { IsSuccess: false } doctor
                ? LastUsefulLine(doctor.StandardError) ??
                    LastUsefulLine(doctor.StandardOutput)
                : null;
        EnvironmentActionTextBlock.Text = _configInspection?.RequiresMigration == true
            ? _configInspection.HasNamedVolume
                ? "下一步：点击“备份与迁移”。此旧版 named volume 需要一次性使用 Docker 导出。"
                : "下一步：点击“备份与迁移”。程序会先备份，再复用已有向量完成升级。"
            : runtimeFailureAction ??
                (!string.IsNullOrWhiteSpace(doctorFailure)
                    ? $"配置检查：{doctorFailure}。可展开日志查看详情。"
                    : snapshot.NextAction);
        ApplyEnvironmentItem(
            EnvironmentPythonBorder,
            EnvironmentPythonStatusTextBlock,
            EnvironmentPythonDetailTextBlock,
            snapshot.Python
        );
        ApplyEnvironmentItem(
            EnvironmentDependenciesBorder,
            EnvironmentDependenciesStatusTextBlock,
            EnvironmentDependenciesDetailTextBlock,
            snapshot.Dependencies
        );
        ApplyEnvironmentItem(
            EnvironmentApiKeyBorder,
            EnvironmentApiKeyStatusTextBlock,
            EnvironmentApiKeyDetailTextBlock,
            snapshot.ApiKey
        );
        ApplyEnvironmentItem(
            EnvironmentImageRootBorder,
            EnvironmentImageRootStatusTextBlock,
            EnvironmentImageRootDetailTextBlock,
            snapshot.ImageRoot
        );
        ApplyEnvironmentItem(
            EnvironmentWorkspaceBorder,
            EnvironmentWorkspaceStatusTextBlock,
            EnvironmentWorkspaceDetailTextBlock,
            snapshot.Workspace
        );
        ApplyEnvironmentItem(
            EnvironmentResultsBorder,
            EnvironmentResultsStatusTextBlock,
            EnvironmentResultsDetailTextBlock,
            snapshot.ResultsDirectory
        );
        RepairEnvironmentButton.Content = runtimeReady
            ? "修复 / 重新安装环境"
            : "自动修复运行环境";
        UpdateWorkspaceMigrationPresentation(workspace);
    }

    private static void ApplyEnvironmentItem(
        Border border,
        TextBlock statusText,
        TextBlock detailText,
        EnvironmentCheckItem item)
    {
        var colors = item.State switch
        {
            EnvironmentCheckState.Ready => (
                Background: Color.FromRgb(240, 253, 244),
                Border: Color.FromRgb(187, 247, 208),
                Foreground: Color.FromRgb(21, 128, 61)
            ),
            EnvironmentCheckState.Pending => (
                Background: Color.FromRgb(239, 246, 255),
                Border: Color.FromRgb(191, 219, 254),
                Foreground: Color.FromRgb(29, 78, 216)
            ),
            EnvironmentCheckState.Checking => (
                Background: Color.FromRgb(248, 250, 252),
                Border: Color.FromRgb(226, 232, 240),
                Foreground: Color.FromRgb(71, 85, 105)
            ),
            _ => (
                Background: Color.FromRgb(255, 247, 237),
                Border: Color.FromRgb(254, 215, 170),
                Foreground: Color.FromRgb(180, 83, 9)
            ),
        };
        border.Background = new SolidColorBrush(colors.Background);
        border.BorderBrush = new SolidColorBrush(colors.Border);
        statusText.Foreground = new SolidColorBrush(colors.Foreground);
        statusText.Text = item.Status;
        detailText.Text = item.Detail;
    }

    private LauncherConfig? CreateEnvironmentValidationDraft(
        string imageRoot,
        string workspace,
        string results)
    {
        if (string.IsNullOrWhiteSpace(imageRoot) ||
            string.IsNullOrWhiteSpace(workspace) ||
            string.IsNullOrWhiteSpace(results))
        {
            return null;
        }

        var library = (_libraryDraft ??
            _config?.DefaultLibrary ??
            _configService.CreateLibraryTemplate()).Copy();
        library.Name = string.IsNullOrWhiteSpace(SetupLibraryNameTextBox.Text)
            ? string.IsNullOrWhiteSpace(library.Name) ? "新图库" : library.Name
            : SetupLibraryNameTextBox.Text.Trim();
        library.ImageRoot = imageRoot;
        library.WorkspaceDirectory = workspace;

        if (_config is null)
        {
            library.Enabled = true;
            return new LauncherConfig
            {
                SchemaVersion = LauncherConfig.CurrentSchemaVersion,
                PythonExecutable = NormalizeOptionalPath(
                    SetupPythonExecutableTextBox.Text
                ),
                ResultsDirectory = results,
                DefaultLibraryId = library.Id,
                Libraries = [library],
            };
        }

        var draft = CopyConfig(_config);
        draft.PythonExecutable = NormalizeOptionalPath(
            SetupPythonExecutableTextBox.Text
        );
        draft.ResultsDirectory = results;
        var index = draft.Libraries.FindIndex(item => string.Equals(
            item.Id,
            library.Id,
            StringComparison.OrdinalIgnoreCase
        ));
        if (index < 0)
        {
            draft.Libraries.Add(library);
        }
        else
        {
            draft.Libraries[index] = library;
        }
        return draft;
    }

    private bool TryValidateEnvironmentPathsForSave(out string error)
    {
        var imageRoot = SetupImageRootTextBox.Text.Trim();
        var workspace = SetupWorkspaceTextBox.Text.Trim();
        var results = SetupResultsTextBox.Text.Trim();
        var draft = CreateEnvironmentValidationDraft(imageRoot, workspace, results);
        if (draft is null)
        {
            error = "图片目录、Workspace 和搜索结果目录不能为空。";
            return false;
        }

        var probe = ProbeEnvironmentPaths(imageRoot, workspace, results, draft);
        _environmentPathProbe = probe;
        ApplyEnvironmentSnapshot();
        if (!string.IsNullOrWhiteSpace(probe.LayoutError))
        {
            error = probe.LayoutError;
            return false;
        }
        if (!probe.ImageRootExists)
        {
            error = "图片目录不存在，请重新选择。";
            return false;
        }
        if (!probe.ImageRootReadable)
        {
            error = "图片目录无法读取，请检查当前用户权限或重新选择。";
            return false;
        }
        if (!probe.WorkspaceWritable)
        {
            error = probe.WorkspaceExists
                ? "Workspace 目录不可写，请检查当前用户权限。"
                : "Workspace 的父目录不可写，无法安全创建目录。";
            return false;
        }
        if (!probe.ResultsWritable)
        {
            error = probe.ResultsExists
                ? "搜索结果目录不可写，请检查当前用户权限。"
                : "搜索结果目录的父目录不可写，无法安全创建目录。";
            return false;
        }
        error = string.Empty;
        return true;
    }

    private void QueueEnvironmentPathProbe()
    {
        _environmentPathProbeCancellation?.Cancel();
        var cancellation = new CancellationTokenSource();
        _environmentPathProbeCancellation = cancellation;
        _ = RunQueuedEnvironmentPathProbeAsync(cancellation);
    }

    private async Task RunQueuedEnvironmentPathProbeAsync(
        CancellationTokenSource cancellation)
    {
        try
        {
            await RefreshEnvironmentPathProbeAsync(
                TimeSpan.FromMilliseconds(400),
                cancellation.Token
            );
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            AppendLog($"目录状态检测失败：{exception.Message}", true);
        }
        finally
        {
            if (ReferenceEquals(_environmentPathProbeCancellation, cancellation))
            {
                _environmentPathProbeCancellation = null;
            }
            cancellation.Dispose();
        }
    }

    private async Task RefreshEnvironmentPathProbeAsync(
        TimeSpan delay,
        CancellationToken cancellationToken)
    {
        var imageRoot = SetupImageRootTextBox.Text.Trim();
        var workspace = SetupWorkspaceTextBox.Text.Trim();
        var results = SetupResultsTextBox.Text.Trim();
        var validationDraft = CreateEnvironmentValidationDraft(
            imageRoot,
            workspace,
            results
        );
        if (delay > TimeSpan.Zero)
        {
            await Task.Delay(delay, cancellationToken);
        }

        var probe = await Task.Run(
            () => ProbeEnvironmentPaths(
                imageRoot,
                workspace,
                results,
                validationDraft
            ),
            cancellationToken
        );
        cancellationToken.ThrowIfCancellationRequested();
        if (!string.Equals(
                SetupImageRootTextBox.Text.Trim(),
                imageRoot,
                StringComparison.Ordinal
            ) ||
            !string.Equals(
                SetupWorkspaceTextBox.Text.Trim(),
                workspace,
                StringComparison.Ordinal
            ) ||
            !string.Equals(
                SetupResultsTextBox.Text.Trim(),
                results,
                StringComparison.Ordinal
            ))
        {
            return;
        }
        _environmentPathProbe = probe;
        ApplyEnvironmentSnapshot();
    }

    private EnvironmentPathProbeResult ProbeEnvironmentPaths(
        string imageRoot,
        string workspace,
        string results,
        LauncherConfig? validationDraft)
    {
        string? layoutError = null;
        if (validationDraft is not null)
        {
            try
            {
                _configService.ValidateDraft(validationDraft);
            }
            catch (Exception exception) when (
                exception is InvalidDataException or ArgumentException or
                    NotSupportedException or PathTooLongException
            )
            {
                layoutError = exception.Message;
            }
        }

        var imageRootExists = Directory.Exists(imageRoot);
        var imageRootReadable = imageRootExists &&
            FirstUsePathProbe.IsReadableDirectory(imageRoot);
        var workspaceExists = Directory.Exists(workspace);
        var resultsExists = Directory.Exists(results);
        return new EnvironmentPathProbeResult(
            imageRoot,
            workspace,
            results,
            imageRootExists,
            imageRootReadable,
            workspaceExists,
            string.IsNullOrWhiteSpace(layoutError) &&
                FirstUsePathProbe.IsWritableDirectoryOrParent(workspace),
            workspaceExists && File.Exists(
                Path.Combine(workspace, "image_collection.meta.json")
            ),
            resultsExists,
            string.IsNullOrWhiteSpace(layoutError) &&
                FirstUsePathProbe.IsWritableDirectoryOrParent(results),
            layoutError
        );
    }

    private void UpdateWorkspaceMigrationPresentation(string workspace)
    {
        var hasIndex = _environmentPathProbe is not null &&
            string.Equals(
                _environmentPathProbe.Workspace,
                workspace,
                StringComparison.Ordinal
            ) &&
            _environmentPathProbe.WorkspaceHasIndex;
        if (_configInspection?.RequiresMigration == true)
        {
            WorkspaceMigrationStatusTextBlock.Text = _workspaceMigrationStatusOverride ??
                (_configInspection.HasNamedVolume
                    ? "旧版 Workspace：需要一次性使用 Docker 导出 named volume。"
                    : "旧版 Workspace：将先备份，再执行零 API 升级。");
            BackupMigrationButton.IsEnabled = _workspaceMigrationService is not null;
            return;
        }
        if (!string.IsNullOrWhiteSpace(_workspaceMigrationStatusOverride))
        {
            WorkspaceMigrationStatusTextBlock.Text = _workspaceMigrationStatusOverride;
            BackupMigrationButton.IsEnabled = hasIndex &&
                _workspaceMigrationService is not null &&
                _config is not null;
            return;
        }
        if (string.IsNullOrWhiteSpace(workspace) || !hasIndex)
        {
            WorkspaceMigrationStatusTextBlock.Text =
                "Workspace：新建图库无需迁移；检测到旧版索引时会提示先备份。";
            BackupMigrationButton.IsEnabled = false;
            return;
        }

        WorkspaceMigrationStatusTextBlock.Text =
            "Workspace：已检测到现有索引；可先预检、备份，再执行兼容迁移。";
        BackupMigrationButton.IsEnabled = _workspaceMigrationService is not null &&
            _config is not null;
    }

    private void AppendRuntimeBootstrapLog(RuntimeBootstrapResult result)
    {
        AppendLog($"运行环境：{result.Message}", !result.IsSuccess);
        if (!string.IsNullOrWhiteSpace(result.RecommendedAction))
        {
            AppendLog($"建议：{result.RecommendedAction}", !result.IsSuccess);
        }
    }

    private static string BuildRuntimeDiagnosticText(RuntimeBootstrapResult? result)
    {
        if (result is null)
        {
            return "尚未执行环境检查。";
        }

        var lines = new List<string> { result.Message };
        if (!string.IsNullOrWhiteSpace(result.PythonExecutable))
        {
            lines.Add($"Python：{BuildPythonDetail(result)}");
        }
        if (result.Dependencies.Count > 0)
        {
            lines.Add($"依赖：{BuildDependencyDetail(result.Dependencies)}");
        }
        if (!string.IsNullOrWhiteSpace(result.RecommendedAction))
        {
            lines.Add($"下一步：{result.RecommendedAction}");
        }
        return string.Join(Environment.NewLine, lines);
    }

    private static string BuildPythonDetail(RuntimeBootstrapResult? result)
    {
        if (result is null)
        {
            return "隔离 Python 已就绪";
        }
        var identity = string.Join(
            " · ",
            new[] { result.PythonVersion, result.PythonArchitecture }
                .Where(value => !string.IsNullOrWhiteSpace(value))
        );
        return string.IsNullOrWhiteSpace(identity)
            ? result.PythonExecutable ?? "隔离 Python 已就绪"
            : identity;
    }

    private static string BuildDependencyDetail(
        IReadOnlyList<RuntimeDependencyStatus> dependencies)
    {
        var values = dependencies
            .Where(dependency => dependency.Imported)
            .Select(dependency => string.IsNullOrWhiteSpace(dependency.Version)
                ? dependency.Name
                : $"{dependency.Name} {dependency.Version}")
            .ToList();
        return values.Count == 0 ? "运行依赖已就绪" : string.Join(" · ", values);
    }

    private static string? NormalizeOptionalPath(string value)
    {
        var normalized = value.Trim();
        return normalized.Length == 0 ? null : normalized;
    }

    private static bool IsAutomaticallyRepairable(RuntimeBootstrapResult? result) =>
        result is { IsSuccess: false } && result.Code is
            "runtime_not_installed" or
            "runtime_outdated" or
            "runtime_corrupt" or
            "venv_unusable" or
            "venv_pip_unusable" or
            "dependency_import_failed" or
            "dependency_probe_invalid";

    private static void EnsureZeroApiMigration(int apiRequests, string operation)
    {
        if (apiRequests != 0)
        {
            throw new InvalidDataException(
                $"{operation}报告异常：已有向量的备份与迁移不得调用模型 API。"
            );
        }
    }

    private async void Stats_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetIndexLibrary(out var library))
        {
            ShowValidation("请选择需要查看状态的图库。");
            return;
        }

        var backendJob = await RunBackendJobAsync(
            "正在读取 Collection 状态…",
            "stats",
            new Dictionary<string, object?>
            {
                ["library_id"] = library.Id,
            }
        );
        if (backendJob is not null)
        {
            if (HandleBackendJob(backendJob, "Collection 状态已刷新") &&
                backendJob.Result is JsonElement resultElement)
            {
                StatsOutputTextBox.Text = PrettyJson(resultElement);
            }
            return;
        }

        var result = await RunCommandAsync(
            "正在读取 Collection 状态…",
            "stats",
            environment: CreateLibraryEnvironment(library.Id)
        );
        if (result is null)
        {
            return;
        }

        StatsOutputTextBox.Text = result.StandardOutput.Trim();
        HandleCommandResult(result, "Collection 状态已刷新", allowPartial: false);
    }

    private void OpenResults_Click(object sender, RoutedEventArgs e)
    {
        if (_config is null)
        {
            ShowValidation("请先初始化图片库。");
            return;
        }

        OpenPath(_config.ResultsDirectory);
    }

    private async void Index_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetIndexLibrary(out var library))
        {
            ShowValidation("请选择需要建立索引的图库。");
            return;
        }

        var tags = SplitTokens(IndexTagsTextBox.Text);
        if (ClearTagsCheckBox.IsChecked == true && tags.Count > 0)
        {
            ShowValidation("清除人工标签时不能同时填写新标签。");
            return;
        }
        IReadOnlyList<string>? requestedTags = ClearTagsCheckBox.IsChecked == true
            ? Array.Empty<string>()
            : tags.Count > 0
                ? tags
                : null;

        if (IndexAndAutoTagCheckBox.IsChecked == true)
        {
            if (!await TryStartBackendAsync(showFailure: false))
            {
                ShowValidation(
                    "索引并智能标注需要常驻后端；当前后端不可用。组合操作不会退化为两个顺序任务。"
                );
                return;
            }
            if (_backendHost?.SupportsIndexAndAutoTag != true)
            {
                ShowValidation(
                    "当前常驻后端不支持“索引并智能标注”，请更新桌面应用和原生后端。不会顺序执行两个普通任务。"
                );
                return;
            }
            if (!TryReadAutoTagExecutionOptions(
                requireExternalConfirmation: true,
                out var autoTagOptions
            ))
            {
                return;
            }
            if (!ConfirmAutoTagCost(
                autoTagOptions.Model,
                autoTagOptions.MaxImages,
                autoTagOptions.MaxBudgetCny,
                "索引期间将最多",
                "确认索引并智能标注"
            ))
            {
                return;
            }

            var request = new IndexAndAutoTagRequest
            {
                LibraryId = library.Id,
                Recursive = IndexRecursiveCheckBox.IsChecked == true,
                VerifyHash = VerifyHashCheckBox.IsChecked == true,
                Tags = requestedTags,
                Model = autoTagOptions.Model,
                MaxImages = autoTagOptions.MaxImages,
                MaxBudgetCny = autoTagOptions.MaxBudgetCny,
                ExternalProcessingConfirmed = true,
            };
            var combinedJob = await RunBackendSubmissionAsync(
                "正在并行建立索引并生成智能标签…",
                "index_and_auto_tag",
                async backend => await backend.ApiClient.StartIndexAndAutoTagAsync(request),
                _ => ExternalProcessingConfirmCheckBox.IsChecked = false
            );
            if (combinedJob is null)
            {
                ShowValidation(
                    "索引并智能标注任务未提交；不会改为顺序执行索引和自动标注。"
                );
                return;
            }
            HandleBackendJob(combinedJob, "索引和智能标注完成");
            return;
        }

        var backendJob = await RunBackendJobAsync(
            "正在扫描并建立图片索引…",
            "index",
            new Dictionary<string, object?>
            {
                ["library_id"] = library.Id,
                ["recursive"] = IndexRecursiveCheckBox.IsChecked == true,
                ["verify_hash"] = VerifyHashCheckBox.IsChecked == true,
                ["tags"] = requestedTags,
            }
        );
        if (backendJob is not null)
        {
            HandleBackendJob(backendJob, "索引完成");
            return;
        }

        var arguments = new List<string>();
        arguments.AddRange(tags);
        if (IndexRecursiveCheckBox.IsChecked != true)
        {
            arguments.Add("--no-recursive");
        }
        if (VerifyHashCheckBox.IsChecked == true)
        {
            arguments.Add("--verify-hash");
        }
        if (ClearTagsCheckBox.IsChecked == true)
        {
            arguments.Add("--clear-tags");
        }

        var result = await RunCommandAsync(
            "正在扫描并建立图片索引…",
            "index",
            arguments,
            CreateLibraryEnvironment(library.Id)
        );
        if (result is not null)
        {
            HandleCommandResult(result, "索引完成", allowPartial: true);
        }
    }

    private void IndexAndAutoTagToggle_Click(object sender, RoutedEventArgs e)
    {
        IndexActionButton.Content = IndexAndAutoTagCheckBox.IsChecked == true
            ? "索引并智能标注"
            : "建立 / 更新索引";
    }

    private async void SyncPreview_Click(object sender, RoutedEventArgs e) =>
        await RunSyncAsync(dryRun: true);

    private async void Sync_Click(object sender, RoutedEventArgs e)
    {
        var answer = MessageBox.Show(
            this,
            "同步会删除 Collection 中源文件已经不存在的记录。建议先执行同步预演。是否继续？",
            "确认同步",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning
        );
        if (answer == MessageBoxResult.Yes)
        {
            await RunSyncAsync(dryRun: false);
        }
    }

    private async Task RunSyncAsync(bool dryRun)
    {
        if (!TryGetIndexLibrary(out var library))
        {
            ShowValidation("请选择需要同步的图库。");
            return;
        }

        var backendJob = await RunBackendJobAsync(
            dryRun ? "正在预演同步…" : "正在同步图片目录…",
            "sync",
            new Dictionary<string, object?>
            {
                ["library_id"] = library.Id,
                ["recursive"] = IndexRecursiveCheckBox.IsChecked == true,
                ["verify_hash"] = VerifyHashCheckBox.IsChecked == true,
                ["dry_run"] = dryRun,
                ["allow_scope_change"] = false,
            }
        );
        if (backendJob is not null)
        {
            HandleBackendJob(
                backendJob,
                dryRun ? "同步预演完成" : "同步完成"
            );
            return;
        }

        var arguments = new List<string>();
        if (dryRun)
        {
            arguments.Add("--dry-run");
        }
        if (IndexRecursiveCheckBox.IsChecked != true)
        {
            arguments.Add("--no-recursive");
        }
        if (VerifyHashCheckBox.IsChecked == true)
        {
            arguments.Add("--verify-hash");
        }

        var result = await RunCommandAsync(
            dryRun ? "正在预演同步…" : "正在同步图片目录…",
            "sync",
            arguments,
            CreateLibraryEnvironment(library.Id)
        );
        if (result is not null)
        {
            HandleCommandResult(
                result,
                dryRun ? "同步预演完成" : "同步完成",
                allowPartial: true
            );
        }
    }

    private async void Search_Click(object sender, RoutedEventArgs e)
    {
        if (_config is null)
        {
            ShowValidation("请先初始化并建立索引。");
            return;
        }
        var libraryIds = GetSelectedSearchLibraryIds();
        if (libraryIds.Count == 0)
        {
            ShowValidation("请选择至少一个已启用图库进行搜索。");
            return;
        }
        if (!TryParseInteger(TopKTextBox.Text, 1, 1000, out var topK))
        {
            ShowValidation("返回数量必须是 1 到 1000 之间的整数。");
            return;
        }

        var searchText = SearchTextBox.Text.Trim();
        var queryImage = QueryImageTextBox.Text.Trim();
        var mode = SearchModeComboBox.SelectedIndex;
        var semanticTextSearchEnabled =
            mode != 0 || SemanticTextSearchCheckBox.IsChecked != false;
        var tagOnlySearch = mode == 0 && !semanticTextSearchEnabled;
        var imageWeight = 0.5;
        var textWeight = 0.5;
        string command;
        var arguments = new List<string>();
        switch (mode)
        {
            case 0:
                if (string.IsNullOrWhiteSpace(searchText))
                {
                    ShowValidation("文字搜索需要输入查询内容。");
                    return;
                }
                command = "search";
                arguments.Add("--text");
                arguments.Add(searchText);
                if (tagOnlySearch)
                {
                    arguments.Add("--search-mode");
                    arguments.Add("tags");
                }
                break;
            case 1:
                if (!File.Exists(queryImage))
                {
                    ShowValidation("图片搜索需要选择存在的查询图片。");
                    return;
                }
                command = "search-image";
                arguments.Add(queryImage);
                break;
            default:
                if (string.IsNullOrWhiteSpace(searchText) || !File.Exists(queryImage))
                {
                    ShowValidation("图文搜索需要同时提供查询文字和图片。");
                    return;
                }
                if (
                    !TryParseDouble(ImageWeightTextBox.Text, out imageWeight) ||
                    !TryParseDouble(TextWeightTextBox.Text, out textWeight) ||
                    imageWeight < 0 ||
                    textWeight < 0 ||
                    imageWeight + textWeight <= 0
                )
                {
                    ShowValidation("图像和文字权重必须为非负数字，且不能同时为 0。");
                    return;
                }
                command = "search-mix";
                arguments.Add(queryImage);
                arguments.Add(searchText);
                arguments.Add("--image-weight");
                arguments.Add(imageWeight.ToString(CultureInfo.InvariantCulture));
                arguments.Add("--text-weight");
                arguments.Add(textWeight.ToString(CultureInfo.InvariantCulture));
                break;
        }

        arguments.Add("--tk");
        arguments.Add(topK.ToString(CultureInfo.InvariantCulture));
        var tags = SplitTokens(SearchTagsTextBox.Text);
        if (tags.Count > 0)
        {
            arguments.Add("--tags");
            arguments.AddRange(tags);
        }
        arguments.Add("--tag-mode");
        arguments.Add(GetSelectedComboValue(TagModeComboBox, "all"));
        if (IncludeSelfCheckBox.IsChecked == true)
        {
            arguments.Add("--include-self");
        }
        var showLowConfidence =
            !tagOnlySearch && ShowLowConfidenceCheckBox.IsChecked == true;
        var diversifyResults = DiversifyResultsCheckBox.IsChecked != false;
        if (showLowConfidence)
        {
            arguments.Add("--show-low-confidence");
        }
        if (!diversifyResults)
        {
            arguments.Add("--show-all-series");
        }

        if (tagOnlySearch && libraryIds.Count > 1)
        {
            if (
                !await TryStartBackendAsync(showFailure: false) ||
                _backendHost?.SupportsTagOnlySearch != true
            )
            {
                ShowValidation(
                    "跨图库纯标签搜索需要新版常驻后端。请先在“状态与诊断”中修复后端，或只选择一个图库。"
                );
                return;
            }
        }

        var configSnapshot = _config;
        BackendStagedQueryFile? stagedFile = null;
        BackendJob? backendJob = null;
        var usePersistentBackend =
            !tagOnlySearch ||
            (_backendHost?.IsRunning == true && _backendHost.SupportsTagOnlySearch);
        var runningSearchStatus = tagOnlySearch
            ? "正在按标签检索…"
            : "正在进行向量检索…";
        if (usePersistentBackend)
        {
            backendJob = await RunBackendJobAsync(
                runningSearchStatus,
                "search",
                async backend =>
                {
                    if (mode is 1 or 2)
                    {
                        stagedFile = await backend.StageQueryFileAsync(queryImage);
                    }

                    var parameters = new Dictionary<string, object?>
                    {
                        ["library_ids"] = libraryIds,
                        ["text"] = mode is 0 or 2 ? searchText : null,
                        ["image"] = stagedFile?.BackendPath,
                        ["top_k"] = topK,
                        ["tags"] = tags,
                        ["tag_mode"] = GetSelectedComboValue(TagModeComboBox, "all"),
                        ["image_weight"] = imageWeight,
                        ["text_weight"] = textWeight,
                        ["include_self"] = IncludeSelfCheckBox.IsChecked == true,
                    };
                    UpdateBackendSearchCapabilities(backend);
                    BackendSearchRequestOptions.AddSearchMode(
                        parameters,
                        semanticTextSearchEnabled,
                        backend.SupportsTagOnlySearch
                    );
                    BackendSearchRequestOptions.AddLowConfidenceOverride(
                        parameters,
                        showLowConfidence,
                        backend.SupportsLowConfidenceOverride
                    );
                    BackendSearchRequestOptions.AddResultDiversity(
                        parameters,
                        diversifyResults,
                        backend.SupportsResultDiversity
                    );
                    return parameters;
                }
            );
        }
        if (backendJob is not null)
        {
            try
            {
                if (!HandleBackendJob(backendJob, "搜索完成"))
                {
                    return;
                }
                if (backendJob.Result is not JsonElement backendResult)
                {
                    StatusTextBlock.Text = "搜索完成，但后端没有返回结果清单";
                    return;
                }

                var backendSession = await _resultLoader.LoadFromCommandOutputAsync(
                    configSnapshot,
                    backendResult.GetRawText()
                );
                if (backendSession is null)
                {
                    StatusTextBlock.Text = "搜索完成，但没有找到结果清单";
                    return;
                }
                ShowResultSession(backendSession);
                return;
            }
            catch (Exception exception)
            {
                ShowOperationError("常驻后端搜索失败", exception);
                return;
            }
            finally
            {
                if (stagedFile is not null)
                {
                    TryDeleteFile(stagedFile.HostPath);
                }
            }
        }

        if (libraryIds.Count > 1)
        {
            ShowValidation(
                "跨图库搜索需要常驻后端。当前后端不可用，为避免错误地只搜索一个图库，已停止 PowerShell 回退。"
            );
            return;
        }

        var result = await RunCommandAsync(
            runningSearchStatus,
            command,
            arguments,
            CreateLibraryEnvironment(libraryIds[0])
        );
        if (result is null || !HandleCommandResult(result, "搜索完成", allowPartial: false))
        {
            return;
        }

        try
        {
            var session = await _resultLoader.LoadFromCommandOutputAsync(
                configSnapshot,
                result.StandardOutput
            );
            if (session is null)
            {
                StatusTextBlock.Text = "搜索完成，但没有找到结果清单";
                return;
            }
            ShowResultSession(session);
        }
        catch (Exception exception)
        {
            ShowOperationError("结果加载失败", exception);
        }
    }

    private void SearchModeComboBox_SelectionChanged(
        object sender,
        SelectionChangedEventArgs e)
    {
        if (IsLoaded)
        {
            UpdateSearchModePresentation();
        }
    }

    private void SemanticTextSearchCheckBox_Changed(
        object sender,
        RoutedEventArgs e)
    {
        if (IsLoaded)
        {
            UpdateSearchModePresentation();
        }
    }

    private void UpdateSearchModePresentation()
    {
        var mode = Math.Max(0, SearchModeComboBox.SelectedIndex);
        var tagOnlySearch =
            mode == 0 && SemanticTextSearchCheckBox.IsChecked == false;
        SemanticTextSearchCheckBox.Visibility = mode == 0
            ? Visibility.Visible
            : Visibility.Collapsed;
        SearchTextPanel.Visibility = mode is 0 or 2
            ? Visibility.Visible
            : Visibility.Collapsed;
        SearchImagePanel.Visibility = mode is 1 or 2
            ? Visibility.Visible
            : Visibility.Collapsed;
        SearchWeightPanel.Visibility = mode == 2
            ? Visibility.Visible
            : Visibility.Collapsed;
        IncludeSelfCheckBox.Visibility = mode is 1 or 2
            ? Visibility.Visible
            : Visibility.Collapsed;
        ShowLowConfidenceCheckBox.Visibility = tagOnlySearch
            ? Visibility.Collapsed
            : Visibility.Visible;
        SearchTextFieldLabel.Text = tagOnlySearch ? "标签关键词" : "文字描述";
        SearchModeHintTextBlock.Text = mode switch
        {
            0 when tagOnlySearch =>
                "仅匹配图库中已有的标签，不调用大模型；输入“原”或“神”都能命中“原神”。",
            0 => "输入自然语言描述，调用向量模型查找语义相近的图片。",
            1 => "选择一张图片，查找视觉内容最相似的结果。",
            _ => "同时提供文字和图片，并通过权重控制两者影响。",
        };
    }

    private async void ReloadResults_Click(object sender, RoutedEventArgs e)
    {
        if (_config is null)
        {
            return;
        }

        try
        {
            var session = await _resultLoader.LoadLatestAsync(_config);
            if (session is not null)
            {
                ShowResultSession(session);
            }
        }
        catch (Exception exception)
        {
            ShowOperationError("结果加载失败", exception);
        }
    }

    private async void CacheClear_Click(object sender, RoutedEventArgs e)
    {
        var answer = MessageBox.Show(
            this,
            "确定清空查询向量缓存吗？图片索引不会被删除。",
            "清空缓存",
            MessageBoxButton.YesNo,
            MessageBoxImage.Question
        );
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }
        if (!TryGetIndexLibrary(out var library))
        {
            ShowValidation("请选择需要清理缓存的图库。");
            return;
        }

        var backendJob = await RunBackendJobAsync(
            "正在清空查询缓存…",
            "cache_clear",
            new Dictionary<string, object?>
            {
                ["library_id"] = library.Id,
            }
        );
        if (backendJob is not null)
        {
            HandleBackendJob(backendJob, "查询缓存已清空");
            return;
        }

        var result = await RunCommandAsync(
            "正在清空查询缓存…",
            "cache-clear",
            environment: CreateLibraryEnvironment(library.Id)
        );
        if (result is not null)
        {
            HandleCommandResult(result, "查询缓存已清空", allowPartial: false);
        }
    }

    private async void CleanPreview_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetIndexLibrary(out var library))
        {
            ShowValidation("请选择需要清理结果的图库。");
            return;
        }
        var backendJob = await RunBackendJobAsync(
            "正在预览旧结果清理…",
            "clean_results",
            new Dictionary<string, object?>
            {
                ["library_id"] = library.Id,
                ["days"] = 7,
                ["dry_run"] = true,
            }
        );
        if (backendJob is not null)
        {
            if (HandleBackendJob(backendJob, "清理预演完成") &&
                backendJob.Result is JsonElement resultElement)
            {
                StatsOutputTextBox.Text = PrettyJson(resultElement);
            }
            return;
        }

        var result = await RunCommandAsync(
            "正在预览旧结果清理…",
            "clean",
            ["7", "--dry-run"],
            CreateLibraryEnvironment(library.Id)
        );
        if (result is not null)
        {
            StatsOutputTextBox.Text = result.StandardOutput.Trim();
            HandleCommandResult(result, "清理预演完成", allowPartial: false);
        }
    }

    private async void Clean_Click(object sender, RoutedEventArgs e)
    {
        var answer = MessageBox.Show(
            this,
            "确定删除 7 天以前的搜索结果目录吗？",
            "清理旧结果",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning
        );
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }
        if (!TryGetIndexLibrary(out var library))
        {
            ShowValidation("请选择需要清理结果的图库。");
            return;
        }

        var backendJob = await RunBackendJobAsync(
            "正在清理旧结果…",
            "clean_results",
            new Dictionary<string, object?>
            {
                ["library_id"] = library.Id,
                ["days"] = 7,
                ["dry_run"] = false,
            }
        );
        if (backendJob is not null)
        {
            if (HandleBackendJob(backendJob, "旧结果已清理") &&
                backendJob.Result is JsonElement resultElement)
            {
                StatsOutputTextBox.Text = PrettyJson(resultElement);
            }
            return;
        }

        var result = await RunCommandAsync(
            "正在清理旧结果…",
            "clean",
            ["7"],
            CreateLibraryEnvironment(library.Id)
        );
        if (result is not null)
        {
            StatsOutputTextBox.Text = result.StandardOutput.Trim();
            HandleCommandResult(result, "旧结果已清理", allowPartial: false);
        }
    }

    private async void ResultsList_SelectionChanged(
        object sender,
        SelectionChangedEventArgs e)
    {
        if (ResultsList.SelectedItem is not SearchResultItem item)
        {
            CancelSelectedResultPreview();
            return;
        }

        SelectedPreviewImage.Source = item.Thumbnail;
        SelectedNameText.Text = item.Name;
        SelectedScoreText.Text = item.ScoreText;
        SelectedDiagnosticScoreText.Text = item.DiagnosticScoreText;
        SelectedLibraryText.Text = item.LibraryText;
        SelectedTagsText.Text = item.TagsText;
        SelectedPathText.Text = item.RelativePath;
        await LoadSelectedResultPreviewAsync(item);
    }

    private void ResultsList_MouseDoubleClick(object sender, MouseButtonEventArgs e) =>
        OpenSelectedResult();

    private void OpenSelected_Click(object sender, RoutedEventArgs e) => OpenSelectedResult();

    private void OpenSelectedFolder_Click(object sender, RoutedEventArgs e)
    {
        if (ResultsList.SelectedItem is not SearchResultItem item)
        {
            return;
        }

        var file = File.Exists(item.OriginalPath) ? item.OriginalPath : item.CopiedPath;
        if (!string.IsNullOrWhiteSpace(file))
        {
            OpenPath(Path.GetDirectoryName(file)!);
        }
    }

    private async void Cancel_Click(object sender, RoutedEventArgs e)
    {
        if (!_isBusy)
        {
            return;
        }

        StatusTextBlock.Text = "正在取消任务…";
        await CancelCurrentOperationAsync();
    }

    private void ClearLog_Click(object sender, RoutedEventArgs e)
    {
        while (_pendingLogLines.TryDequeue(out _))
        {
            Interlocked.Decrement(ref _pendingLogLineCount);
        }
        Interlocked.Exchange(ref _droppedLogLineCount, 0);
        LogTextBox.Clear();
        _renderedLogCharacterCount = 0;
    }

    private async Task SaveLibraryDraftAsync()
    {
        if (_config is null || _libraryDraft is null)
        {
            return;
        }

        var libraryName = SetupLibraryNameTextBox.Text.Trim();
        var imageRoot = SetupImageRootTextBox.Text.Trim();
        var workspace = SetupWorkspaceTextBox.Text.Trim();
        var results = SetupResultsTextBox.Text.Trim();
        var pythonExecutable = SetupPythonExecutableTextBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(libraryName))
        {
            ShowValidation("图库名称不能为空。");
            return;
        }
        if (!Directory.Exists(imageRoot))
        {
            ShowValidation("请选择存在的图片目录。");
            return;
        }
        if (string.IsNullOrWhiteSpace(workspace) ||
            string.IsNullOrWhiteSpace(results))
        {
            ShowValidation("Workspace 和搜索结果目录不能为空。");
            return;
        }
        if (!TryValidateEnvironmentPathsForSave(out var pathValidationError))
        {
            ShowValidation(pathValidationError);
            return;
        }
        if (_config.Libraries.Any(library =>
            !string.Equals(library.Id, _libraryDraft.Id, StringComparison.OrdinalIgnoreCase) &&
            string.Equals(library.Name, libraryName, StringComparison.CurrentCultureIgnoreCase)))
        {
            ShowValidation($"图库名称“{libraryName}”已经存在。");
            return;
        }
        if (_config.Libraries.Any(library =>
            !string.Equals(library.Id, _libraryDraft.Id, StringComparison.OrdinalIgnoreCase) &&
            string.Equals(
                library.WorkspaceDirectory,
                workspace,
                StringComparison.OrdinalIgnoreCase
            )))
        {
            ShowValidation("多个图库不能共用同一个 Workspace。");
            return;
        }

        try
        {
            if (!string.IsNullOrWhiteSpace(ApiKeyPasswordBox.Password))
            {
                _apiCredentialService.SaveApiKey(ApiKeyPasswordBox.Password);
                _hasStoredApiKey = true;
                ApiKeyPasswordBox.Clear();
                ApiKeyStatusTextBlock.Text = "API Key 已保存到 Windows 凭据管理器";
            }
            Directory.CreateDirectory(workspace);
            Directory.CreateDirectory(results);
        }
        catch (Exception exception)
        {
            ShowOperationError("无法保存图库配置", exception);
            return;
        }

        var next = CopyConfig(_config);
        next.PythonExecutable = string.IsNullOrWhiteSpace(pythonExecutable)
            ? null
            : pythonExecutable;
        next.ResultsDirectory = results;
        var edited = _libraryDraft.Copy();
        edited.Name = libraryName;
        edited.ImageRoot = imageRoot;
        edited.WorkspaceDirectory = workspace;
        var existingIndex = next.Libraries.FindIndex(library => string.Equals(
            library.Id,
            edited.Id,
            StringComparison.OrdinalIgnoreCase
        ));
        var isNew = existingIndex < 0;
        if (isNew)
        {
            next.Libraries.Add(edited);
        }
        else
        {
            next.Libraries[existingIndex] = edited;
        }

        await ApplyConfigurationChangeAsync(
            next,
            isNew ? $"图库“{edited.Name}”已添加" : $"图库“{edited.Name}”配置已更新",
            edited.Id
        );
    }

    private async Task<bool> ApplyConfigurationChangeAsync(
        LauncherConfig next,
        string successMessage,
        string preferredLibraryId)
    {
        if (_config is null)
        {
            return false;
        }
        if (!EnsureNoActiveBackendTasks("修改图库配置"))
        {
            return false;
        }

        var previous = CopyConfig(_config);
        SetBusy(true, "正在应用图库配置并重建常驻后端…");
        Exception? rollbackFailure = null;
        try
        {
            await StopBackendAsync();
            await _configService.SaveAsync(next);
            _config = next;
            RefreshLibraryControls(preferredLibraryId);

            if (!await TryStartBackendAsync(showFailure: false))
            {
                throw new InvalidOperationException("新图库配置已保存，但常驻后端未能就绪。");
            }

            StatusTextBlock.Text = successMessage;
            return true;
        }
        catch (Exception exception)
        {
            AppendLog($"应用图库配置失败：{exception}", isError: true);
            try
            {
                await StopBackendAsync();
                await _configService.SaveAsync(previous);
                _config = previous;
                RefreshLibraryControls(previous.DefaultLibraryId);
                if (!await TryStartBackendAsync(showFailure: false))
                {
                    throw new InvalidOperationException("旧配置已恢复，但原常驻后端未能重新启动。");
                }
            }
            catch (Exception rollbackException)
            {
                rollbackFailure = rollbackException;
                AppendLog($"恢复旧图库配置失败：{rollbackException}", isError: true);
            }

            var message = rollbackFailure is null
                ? $"配置变更未生效，已恢复旧配置。\n\n{exception.Message}"
                : $"配置变更失败，恢复旧配置时也发生错误。\n\n" +
                    $"原错误：{exception.Message}\n恢复错误：{rollbackFailure.Message}";
            MessageBox.Show(
                this,
                message,
                "图库配置未应用",
                MessageBoxButton.OK,
                MessageBoxImage.Error
            );
            StatusTextBlock.Text = rollbackFailure is null
                ? "图库配置已回滚"
                : "图库配置回滚不完整，请查看日志";
            return false;
        }
        finally
        {
            SetBusy(false, StatusTextBlock.Text);
        }
    }

    private void RefreshLibraryControls(string? preferredLibraryId = null)
    {
        var previousIndexId = (IndexLibraryComboBox.SelectedItem as LibraryChoiceItem)?.LibraryId;
        var previousScope = SearchLibraryScopeComboBox.SelectedItem as LibrarySearchScopeItem;
        _refreshingLibrarySelection = true;
        try
        {
            _libraryItems.Clear();
            _indexLibraryChoices.Clear();
            _searchLibraryScopes.Clear();
            LibraryListBox.SelectedItem = null;

            if (_config is null)
            {
                var template = _configService.CreateLibraryTemplate();
                BeginEditLibrary(template, isNew: true);
                SetupResultsTextBox.Text = _configService.DefaultResultsPath;
                SetupPythonExecutableTextBox.Text = string.Empty;
                ConfigurationTextBlock.Text = "尚未初始化图库";
                return;
            }

            foreach (var library in _config.Libraries)
            {
                _libraryItems.Add(new LibraryListItem
                {
                    Id = library.Id,
                    Name = library.Name,
                    ImageRoot = library.ImageRoot,
                    WorkspaceDirectory = library.WorkspaceDirectory,
                    Enabled = library.Enabled,
                    IsDefault = string.Equals(
                        library.Id,
                        _config.DefaultLibraryId,
                        StringComparison.OrdinalIgnoreCase
                    ),
                });
                if (library.Enabled)
                {
                    _indexLibraryChoices.Add(new LibraryChoiceItem
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
            }

            var enabledCount = _config.EnabledLibraries.Count;
            _searchLibraryScopes.Add(new LibrarySearchScopeItem
            {
                IsAllEnabled = true,
                DisplayName = $"全部启用图库（{enabledCount}）",
            });
            foreach (var library in _config.EnabledLibraries)
            {
                _searchLibraryScopes.Add(new LibrarySearchScopeItem
                {
                    LibraryId = library.Id,
                    DisplayName = library.Name,
                });
            }

            var selectedId = preferredLibraryId ?? _config.DefaultLibraryId;
            LibraryListBox.SelectedItem = _libraryItems.FirstOrDefault(item => string.Equals(
                item.Id,
                selectedId,
                StringComparison.OrdinalIgnoreCase
            )) ?? _libraryItems.FirstOrDefault();
            IndexLibraryComboBox.SelectedItem = _indexLibraryChoices.FirstOrDefault(item =>
                string.Equals(
                    item.LibraryId,
                    previousIndexId ?? selectedId,
                    StringComparison.OrdinalIgnoreCase
                )
            ) ?? _indexLibraryChoices.FirstOrDefault();
            SearchLibraryScopeComboBox.SelectedItem = previousScope?.IsAllEnabled == true
                ? _searchLibraryScopes.FirstOrDefault(item => item.IsAllEnabled)
                : _searchLibraryScopes.FirstOrDefault(item => string.Equals(
                    item.LibraryId,
                    previousScope?.LibraryId ?? _config.DefaultLibraryId,
                    StringComparison.OrdinalIgnoreCase
                )) ?? _searchLibraryScopes.FirstOrDefault();

            SetupResultsTextBox.Text = _config.ResultsDirectory;
            SetupPythonExecutableTextBox.Text = _config.PythonExecutable ?? string.Empty;
            ConfigurationTextBlock.Text =
                $"默认：{_config.DefaultLibrary?.Name ?? "未设置"} · " +
                $"已启用 {enabledCount}/{_config.Libraries.Count} · 原生 Python 后端";

            if (LibraryListBox.SelectedItem is LibraryListItem selectedItem &&
                _config.GetLibrary(selectedItem.Id) is LauncherLibrary selectedLibrary)
            {
                BeginEditLibrary(selectedLibrary);
            }
        }
        finally
        {
            _refreshingLibrarySelection = false;
            RefreshSmartTagLibraryChoices();
        }
    }

    private void BeginEditLibrary(LauncherLibrary library, bool isNew = false)
    {
        _libraryDraft = library.Copy();
        SetupLibraryNameTextBox.Text = library.Name;
        SetupImageRootTextBox.Text = library.ImageRoot;
        SetupWorkspaceTextBox.Text = library.WorkspaceDirectory;
        SetupWorkspaceTextBox.IsReadOnly = false;
        BrowseWorkspaceButton.IsEnabled = true;
        LibraryEditorTitleText.Text = isNew
            ? _config is null ? "初始化首个图库" : "添加新图库"
            : $"编辑图库：{library.Name}";
        SaveLibraryButton.Content = _config is null
            ? "初始化首个图库"
            : isNew ? "添加图库" : "保存图库配置";
        SaveLibraryButton.IsEnabled = true;
        ToggleLibraryButton.Content = library.Enabled ? "停用" : "启用";
    }

    private bool TryGetSelectedConfiguredLibrary(out LauncherLibrary library)
    {
        library = null!;
        if (_config is null || LibraryListBox.SelectedItem is not LibraryListItem selected)
        {
            return false;
        }

        library = _config.GetLibrary(selected.Id)!;
        return library is not null;
    }

    private bool TryGetIndexLibrary(out LauncherLibrary library)
    {
        library = null!;
        if (_config is null ||
            IndexLibraryComboBox.SelectedItem is not LibraryChoiceItem selected)
        {
            return false;
        }

        library = _config.GetLibrary(selected.LibraryId)!;
        return library is not null && library.Enabled;
    }

    private IReadOnlyList<string> GetSelectedSearchLibraryIds()
    {
        if (_config is null ||
            SearchLibraryScopeComboBox.SelectedItem is not LibrarySearchScopeItem scope)
        {
            return [];
        }

        return scope.IsAllEnabled
            ? _config.EnabledLibraries.Select(library => library.Id).ToList()
            : string.IsNullOrWhiteSpace(scope.LibraryId)
                ? []
                : [scope.LibraryId];
    }

    private IReadOnlyDictionary<string, string?> CreateLibraryEnvironment(string libraryId)
    {
        var environment = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase)
        {
            ["ZVEC_LIBRARY_ID"] = libraryId,
        };
        try
        {
            var apiKey = _apiCredentialService.ReadApiKey();
            if (!string.IsNullOrWhiteSpace(apiKey))
            {
                environment["DASHSCOPE_API_KEY"] = apiKey;
            }
        }
        catch (Exception exception)
        {
            AppendLog($"无法从 Windows 凭据管理器读取 API Key：{exception.Message}", isError: true);
        }
        return environment;
    }

    private static LauncherConfig CopyConfig(LauncherConfig source) => new()
    {
        SchemaVersion = LauncherConfig.CurrentSchemaVersion,
        PythonExecutable = source.PythonExecutable,
        ResultsDirectory = source.ResultsDirectory,
        DefaultLibraryId = source.DefaultLibraryId,
        Libraries = source.Libraries.Select(library => library.Copy()).ToList(),
    };

    private async Task<bool> TryStartBackendAsync(bool showFailure)
    {
        try
        {
            return await _backendLifecycle.RunStartupAsync(
                cancellationToken => TryStartBackendCoreAsync(
                    showFailure,
                    cancellationToken
                )
            );
        }
        catch (OperationCanceledException) when (_backendLifecycle.IsShutdownRequested)
        {
            return false;
        }
    }

    private async Task<bool> TryStartBackendCoreAsync(
        bool showFailure,
        CancellationToken cancellationToken)
    {
        if (_config is null)
        {
            return false;
        }

        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (_backendHost?.IsRunning == true)
            {
                if (await IsBackendHealthyAsync(_backendHost, cancellationToken))
                {
                    UpdateLowConfidenceCapability(
                        _backendHost.SupportsLowConfidenceOverride
                    );
                    UpdateTagOnlySearchCapability(_backendHost.SupportsTagOnlySearch);
                    await EnsureBackendJobMonitorAsync(_backendHost, cancellationToken);
                    return true;
                }

                AppendLog("常驻后端健康检查失败，正在重新启动。", isError: true);
                await StopBackendJobMonitorAsync();
                // A failed health probe is not proof that a long-running backend job
                // stopped. Detach first so a transient network failure can never turn
                // this recovery path into a process-tree kill.
                await _backendHost.DetachAsync();
                await _backendHost.DisposeAsync();
                _backendHost = null;
            }

            SetBackendStatus("正在启动常驻后端…", BackendStatusKind.Starting);
            if (_backendHost is not null)
            {
                await StopBackendJobMonitorAsync();
                await _backendHost.DisposeAsync();
            }
            _backendHost = new BackendHostService(
                _config,
                _configService.EnvironmentPath,
                new BackendHostOptions
                {
                    StartupTimeout = TimeSpan.FromSeconds(45),
                    HealthPollInterval = TimeSpan.FromMilliseconds(350),
                },
                _apiCredentialService
            );
            await _backendHost.StartAsync(cancellationToken);
            cancellationToken.ThrowIfCancellationRequested();
            UpdateLowConfidenceCapability(_backendHost.SupportsLowConfidenceOverride);
            UpdateTagOnlySearchCapability(_backendHost.SupportsTagOnlySearch);
            var reattached = _backendHost.ConnectionMode == BackendConnectionMode.Attached;
            var drainOnly = _backendHost.IsDrainOnly;
            SetBackendStatus(
                drainOnly
                    ? $"已重连旧配置后端 · 等待任务完成 · 127.0.0.1:{_backendHost.HostPort}"
                    : reattached
                        ? $"常驻后端已重连 · 127.0.0.1:{_backendHost.HostPort}"
                        : $"常驻后端已就绪 · 127.0.0.1:{_backendHost.HostPort}",
                drainOnly ? BackendStatusKind.Starting : BackendStatusKind.Ready
            );
            AppendLog(
                reattached
                    ? $"已重新连接原生后端实例 {_backendHost.InstanceId}，端口 " +
                        $"{_backendHost.HostPort}；正在恢复后台任务。"
                    : $"原生常驻后端已启动，实例 {_backendHost.InstanceId}，端口 " +
                        $"{_backendHost.HostPort}。",
                isError: false
            );
            await EnsureBackendJobMonitorAsync(_backendHost, cancellationToken);
            if (!_backendHost.IsDrainOnly)
            {
                _modelConfigurationRestartPending = false;
            }
            ApplyEnvironmentSnapshot();
            return true;
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            if (_backendHost is not null)
            {
                await StopBackendJobMonitorAsync();
                await _backendHost.DisposeAsync();
                _backendHost = null;
            }
            UpdateLowConfidenceCapability(false);
            UpdateTagOnlySearchCapability(false);
            return false;
        }
        catch (Exception exception)
        {
            if (_backendHost is not null)
            {
                await StopBackendJobMonitorAsync();
                await _backendHost.DisposeAsync();
                _backendHost = null;
            }
            UpdateLowConfidenceCapability(false);
            UpdateTagOnlySearchCapability(false);
            SetBackendStatus("命令行兼容模式", BackendStatusKind.Fallback);
            AppendLog($"常驻后端不可用：{exception.Message}", isError: true);
            if (showFailure)
            {
                MessageBox.Show(
                    this,
                    $"常驻后端启动失败，将使用命令行兼容模式。\n\n{exception.Message}",
                    "后端启动失败",
                    MessageBoxButton.OK,
                    MessageBoxImage.Warning
                );
            }
            return false;
        }
    }

    private static async Task<bool> IsBackendHealthyAsync(
        BackendHostService backend,
        CancellationToken cancellationToken)
    {
        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(
                cancellationToken
            );
            timeout.CancelAfter(TimeSpan.FromSeconds(2));
            var health = await backend.ApiClient.GetHealthAsync(timeout.Token);
            return health.IsReady;
        }
        catch (Exception exception) when (
            (exception is HttpRequestException or TaskCanceledException or BackendApiException or
                BackendProtocolException) && !cancellationToken.IsCancellationRequested
        )
        {
            return false;
        }
    }

    private Task StopBackendAsync() => _backendLifecycle.RunExclusiveAsync(
        StopBackendCoreAsync
    );

    private async Task StopBackendCoreAsync()
    {
        await StopBackendJobMonitorAsync();
        var backend = _backendHost;
        _backendHost = null;
        UpdateLowConfidenceCapability(false);
        UpdateTagOnlySearchCapability(false);
        if (backend is null)
        {
            SetBackendStatus("常驻后端未启动", BackendStatusKind.Fallback);
            return;
        }

        var finalStatus = "常驻后端已停止";
        try
        {
            // DisposeAsync intentionally detaches an adopted backend. An explicit
            // stop is required here so an idle backend is actually shut down and
            // its registry entry is cleaned up.
            await backend.StopAsync();
            await backend.DisposeAsync();
        }
        catch (InvalidOperationException exception)
        {
            // A job may have been submitted by another window after our last
            // batch snapshot. Preserve that work instead of turning the race into
            // a forced shutdown.
            AppendLog(
                $"常驻后端仍有任务，改为保留后台运行：{exception.Message}",
                isError: false
            );
            await backend.DetachAsync();
            finalStatus = "后台任务仍在运行；下次打开时将自动恢复";
        }
        catch (Exception exception)
        {
            AppendLog($"停止常驻后端失败：{exception.Message}", isError: true);
            finalStatus = "常驻后端状态未知；下次打开时将重新检查";
        }
        finally
        {
            SetBackendStatus(finalStatus, BackendStatusKind.Fallback);
        }
    }

    private Task DetachBackendAsync() => _backendLifecycle.RunExclusiveAsync(
        DetachBackendCoreAsync
    );

    private async Task DetachBackendCoreAsync()
    {
        await StopBackendJobMonitorAsync();
        var backend = _backendHost;
        _backendHost = null;
        UpdateLowConfidenceCapability(false);
        UpdateTagOnlySearchCapability(false);
        if (backend is null)
        {
            return;
        }

        try
        {
            await backend.DetachAsync();
            SetBackendStatus("后台任务将在窗口关闭后继续运行", BackendStatusKind.Fallback);
            AppendLog(
                "已停止窗口监视；后台任务继续运行，下次打开软件时会恢复到“状态与诊断”。",
                isError: false
            );
        }
        catch (Exception exception)
        {
            // Do not fall back to DisposeAsync here: doing so could terminate the
            // jobs that the user explicitly chose to leave running.
            AppendLog($"分离常驻后端失败：{exception.Message}", isError: true);
        }
    }

    private async Task EnsureBackendJobMonitorAsync(
        BackendHostService backend,
        CancellationToken startupCancellationToken)
    {
        if (ReferenceEquals(_backendJobMonitorHost, backend) &&
            _backendJobMonitorCancellation is { IsCancellationRequested: false } &&
            _backendJobMonitorTask?.IsCompleted != true)
        {
            return;
        }

        await StopBackendJobMonitorAsync();
        startupCancellationToken.ThrowIfCancellationRequested();

        var monitorCancellation = new CancellationTokenSource();
        var monitor = new BackendJobBatchMonitor(
            cancellationToken => backend.ApiClient.GetJobsAsync(
                active: null,
                limit: 50,
                cancellationToken: cancellationToken
            ),
            ApplyBackendJobBatchAsync,
            TimeSpan.FromMilliseconds(750),
            (exception, failureCount, _) =>
            {
                if (failureCount == 1 || failureCount % 20 == 0)
                {
                    AppendLog(
                        $"刷新后台任务状态失败，将自动重试：{exception.Message}",
                        isError: true
                    );
                }
                return Task.CompletedTask;
            }
        );
        _backendJobMonitorHost = backend;
        _backendJobMonitorCancellation = monitorCancellation;

        try
        {
            using var recoveryTimeout = CancellationTokenSource.CreateLinkedTokenSource(
                startupCancellationToken,
                monitorCancellation.Token
            );
            recoveryTimeout.CancelAfter(TimeSpan.FromSeconds(3));
            await monitor.RefreshOnceAsync(isRecovery: true, recoveryTimeout.Token);
        }
        catch (OperationCanceledException) when (startupCancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (OperationCanceledException) when (monitorCancellation.IsCancellationRequested)
        {
            return;
        }
        catch (Exception exception)
        {
            // A temporary list failure must not make an otherwise healthy backend
            // unavailable. The monitor keeps retrying after startup completes.
            AppendLog(
                $"暂时无法恢复后台任务状态，将继续重试：{exception.Message}",
                isError: true
            );
        }

        if (!ReferenceEquals(_backendJobMonitorCancellation, monitorCancellation))
        {
            return;
        }
        _backendJobMonitorTask = Task.Run(
            () => monitor.RunAsync(monitorCancellation.Token),
            CancellationToken.None
        );
    }

    private async Task StopBackendJobMonitorAsync()
    {
        var cancellation = _backendJobMonitorCancellation;
        var monitorTask = _backendJobMonitorTask;
        _backendJobMonitorCancellation = null;
        _backendJobMonitorTask = null;
        _backendJobMonitorHost = null;
        if (cancellation is null)
        {
            return;
        }

        cancellation.Cancel();
        if (monitorTask is not null)
        {
            try
            {
                await monitorTask;
            }
            catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
            {
                // Expected when the window closes or the backend is replaced.
            }
            catch (Exception exception)
            {
                AppendLog($"停止后台任务监视时发生错误：{exception.Message}", isError: true);
            }
        }
        cancellation.Dispose();
    }

    private Task ApplyBackendJobBatchAsync(
        BackendJobBatchUpdate update,
        CancellationToken cancellationToken)
    {
        if (Dispatcher.CheckAccess())
        {
            ApplyBackendJobBatch(update);
            return Task.CompletedTask;
        }

        return Dispatcher.InvokeAsync(
            () => ApplyBackendJobBatch(update),
            DispatcherPriority.Background,
            cancellationToken
        ).Task;
    }

    private void ApplyBackendJobBatch(BackendJobBatchUpdate update)
    {
        // The backend returns newest jobs first. Insert in reverse order so new
        // task-center rows retain that order while still updating by stable ID.
        var presentationChanged = false;
        foreach (var job in update.Jobs.Reverse())
        {
            var recoveredStatus = update.IsRecovery
                ? job.IsTerminal || job.NeedsAttention
                    ? "已恢复的最近任务"
                    : "已从后台恢复；任务仍在继续"
                : null;
            presentationChanged |= UpdateTrackedBackendTask(
                job,
                recoveredStatus,
                updatePresentation: false
            );
        }
        if (presentationChanged || update.IsRecovery)
        {
            RefreshTaskCenterPresentation();
        }

        if (_backendHost?.IsDrainOnly == true && update.ActiveCount == 0 &&
            !_drainBackendRestartScheduled && !_isClosing)
        {
            _drainBackendRestartScheduled = true;
            _ = Dispatcher.BeginInvoke(
                new Action(async () => await RestartDrainedBackendAsync()),
                DispatcherPriority.Background
            );
        }
        ScheduleModelConfigurationRestartIfIdle(update.ActiveCount);

        if (!update.IsRecovery)
        {
            return;
        }
        if (update.Jobs.Count == 0)
        {
            AppendLog("后台任务监视已连接，未发现可恢复的任务。", isError: false);
            return;
        }

        StatusTextBlock.Text = update.ActiveCount > 0
            ? $"已恢复 {update.ActiveCount} 个后台任务；任务仍在继续运行"
            : "已恢复最近的后台任务记录";
        AppendLog(
            $"后台任务已恢复 {update.Jobs.Count} 个最近任务，其中 " +
            $"{update.ActiveCount} 个仍在运行；不会自动重放页面操作。",
            isError: false
        );
    }

    private async Task RestartDrainedBackendAsync()
    {
        try
        {
            var backend = _backendHost;
            if (_isClosing || backend?.IsDrainOnly != true)
            {
                return;
            }
            using var healthTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            var health = await backend.ApiClient.GetHealthAsync(healthTimeout.Token);
            if (health.HasActiveWork)
            {
                return;
            }
            AppendLog(
                "旧配置后台任务已完成，正在按当前图库配置重新启动后端。",
                isError: false
            );
            await StopBackendAsync();
            if (await TryStartBackendAsync(showFailure: true))
            {
                _modelConfigurationRestartPending = false;
            }
        }
        catch (Exception exception)
        {
            AppendLog(
                $"旧配置任务完成后重启后端失败：{exception.Message}",
                isError: true
            );
        }
        finally
        {
            _drainBackendRestartScheduled = false;
        }
    }

    private Task<BackendJob?> RunBackendJobAsync(
        string runningStatus,
        string command,
        IReadOnlyDictionary<string, object?> parameters) => RunBackendJobAsync(
            runningStatus,
            command,
            _ => Task.FromResult(parameters)
        );

    private async Task<BackendJob?> RunBackendJobAsync(
        string runningStatus,
        string command,
        Func<BackendHostService, Task<IReadOnlyDictionary<string, object?>>> parameterFactory)
    {
        return await RunBackendSubmissionAsync(
            runningStatus,
            command,
            async backend =>
            {
                var parameters = await parameterFactory(backend);
                return await backend.ApiClient.SubmitJobAsync(command, parameters);
            }
        );
    }

    private async Task<BackendJob?> RunBackendSubmissionAsync(
        string runningStatus,
        string command,
        Func<BackendHostService, Task<BackendJob?>> submitJob,
        Action<BackendJob>? submittedCallback = null)
    {
        StatusTextBlock.Text = runningStatus;
        BackendJob? submitted = null;
        try
        {
            if (!await TryStartBackendAsync(showFailure: false))
            {
                return null;
            }

            var backend = _backendHost!;
            submitted = await submitJob(backend);
            if (submitted is null)
            {
                return null;
            }
            UpdateTrackedBackendTask(submitted, runningStatus);
            submittedCallback?.Invoke(submitted);
            AppendLog($"已提交常驻后端任务 {submitted.Id} ({command})。", isError: false);
            var completed = await backend.ApiClient.PollJobAsync(
                submitted.Id,
                TimeSpan.FromMilliseconds(500),
                UpdateBackendProgress,
                _backendObservationCancellation.Token
            );
            UpdateTrackedBackendTask(completed);
            return completed;
        }
        catch (OperationCanceledException) when (
            _backendObservationCancellation.IsCancellationRequested || _isClosing
        )
        {
            if (submitted is not null)
            {
                AppendLog(
                    $"已停止监视后台任务 {submitted.Id}；任务本身仍在后台运行。",
                    isError: false
                );
            }
            return submitted;
        }
        catch (Exception exception)
        {
            AppendLog(exception.ToString(), isError: true);
            var failed = new BackendJob
            {
                Id = submitted?.Id ?? string.Empty,
                Command = command,
                Status = "failed",
                Error = new BackendJobError
                {
                    Code = "client_error",
                    Message = exception.Message,
                },
            };
            UpdateTrackedBackendTask(failed, runningStatus);
            return failed;
        }
    }

    private void UpdateBackendProgress(BackendJob job)
    {
        UpdateTrackedBackendTask(job);
        UpdateSmartTagProgress(job);
        var progress = job.Progress;
        if (progress is null)
        {
            StatusTextBlock.Text = job.Status switch
            {
                "queued" => "任务正在排队…",
                "cancelling" => "正在等待当前步骤结束并取消…",
                _ => StatusTextBlock.Text,
            };
            return;
        }

        if (!_lastBackendProgressMessages.TryGetValue(job.Id, out var previousMessage) ||
            !string.Equals(progress.Message, previousMessage, StringComparison.Ordinal))
        {
            _lastBackendProgressMessages[job.Id] = progress.Message;
            AppendLog($"后端：{progress.Message}", isError: false);
        }
        StatusTextBlock.Text = job.Status == "cancelling"
            ? "正在等待当前步骤结束并取消…"
            : progress.Message;
        if (progress.Current.HasValue && progress.Total is > 0)
        {
            BusyProgressBar.IsIndeterminate = false;
            BusyProgressBar.Minimum = 0;
            BusyProgressBar.Maximum = progress.Total.Value;
            BusyProgressBar.Value = Math.Min(
                progress.Current.Value,
                progress.Total.Value
            );
        }
    }

    private bool HasActiveBackendTasks => _backendTaskItems.Any(item => item.IsActive);

    private bool EnsureNoActiveBackendTasks(string operation)
    {
        if (!HasActiveBackendTasks)
        {
            return true;
        }
        ShowValidation(
            $"仍有后台任务正在运行，不能{operation}。请先等待任务完成，或在“状态与诊断”中单独取消相关任务。"
        );
        return false;
    }

    private bool UpdateTrackedBackendTask(
        BackendJob job,
        string? requestedStatus = null,
        bool updatePresentation = true)
    {
        if (string.IsNullOrWhiteSpace(job.Id))
        {
            return false;
        }
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(
                new Action(() => UpdateTrackedBackendTask(
                    job,
                    requestedStatus,
                    updatePresentation
                ))
            );
            return false;
        }

        // A slower overlapping request must never regress a terminal task back
        // to running. This is the only shared-state race between the finite
        // submission poll and the window-level batch monitor.
        if (_terminalBackendTaskIds.Contains(job.Id) && !job.IsTerminal)
        {
            return false;
        }
        if (job.IsTerminal)
        {
            _terminalBackendTaskIds.Add(job.Id);
        }

        var presentationChanged = false;
        if (!_backendTasks.TryGetValue(job.Id, out var item))
        {
            item = new BackendTaskItem();
            _backendTasks.Add(job.Id, item);
            _backendTaskItems.Insert(0, item);
            presentationChanged = true;
        }
        presentationChanged |= item.Update(job, requestedStatus);
        if (updatePresentation && presentationChanged)
        {
            RefreshTaskCenterPresentation();
        }
        return presentationChanged;
    }

    private void RefreshTaskCenterPresentation()
    {
        while (_backendTaskItems.Count > 50)
        {
            var removable = _backendTaskItems.LastOrDefault(candidate => candidate.IsTerminal);
            if (removable is null)
            {
                break;
            }
            _backendTaskItems.Remove(removable);
            _backendTasks.Remove(removable.Id);
            _terminalBackendTaskIds.Remove(removable.Id);
            _lastBackendProgressMessages.Remove(removable.Id);
        }
        TaskLogExpander.IsExpanded = TaskCenterPresentationPolicy.ShouldExpand(
            _backendTaskItems
        );
        UpdateTaskCenterSummary();
    }

    private void UpdateTaskCenterSummary()
    {
        var active = _backendTaskItems.Count(item => item.IsActive);
        var failed = _backendTaskItems.Count(item => item.HasFailures || item.NeedsAttention);
        TaskCenterSummaryTextBlock.Text = active > 0
            ? $"运行中 {active} 个任务" + (failed > 0 ? $" · 需查看 {failed}" : string.Empty)
            : failed > 0
                ? $"已完成 · 需查看 {failed} 个任务"
                : "没有运行中的任务";
    }

    private async void CancelBackendTask_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { CommandParameter: string jobId } ||
            string.IsNullOrWhiteSpace(jobId))
        {
            return;
        }
        await CancelBackendTaskAsync(jobId);
    }

    private void OpenFailureDirectory_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { CommandParameter: string jobId } ||
            _config is null ||
            !_backendTasks.TryGetValue(jobId, out var item))
        {
            return;
        }
        if (!FailureDirectoryResolver.TryResolve(
            _config.ResultsDirectory,
            item.FailureManifestPath,
            item.QuarantinedCount,
            out var failureDirectory
        ))
        {
            StatusTextBlock.Text = "失败目录不可用或已被移动";
            AppendLog($"任务 {jobId} 的失败目录未通过安全校验。", isError: true);
            return;
        }
        OpenPath(failureDirectory);
    }

    private async Task CancelBackendTaskAsync(string jobId)
    {
        if (_backendHost?.IsRunning != true || !_backendTasks.TryGetValue(jobId, out var item))
        {
            return;
        }
        try
        {
            var job = await _backendHost.ApiClient.CancelJobAsync(jobId);
            item.Update(job, "正在取消任务…");
            UpdateTaskCenterSummary();
            StatusTextBlock.Text = "取消请求已发送；其他任务会继续运行";
        }
        catch (BackendApiException exception) when (
            exception.StatusCode == System.Net.HttpStatusCode.Conflict
        )
        {
            try
            {
                UpdateTrackedBackendTask(await _backendHost.ApiClient.GetJobAsync(jobId));
            }
            catch (Exception refreshException)
            {
                AppendLog($"刷新任务 {jobId} 失败：{refreshException.Message}", isError: true);
            }
        }
        catch (Exception exception)
        {
            AppendLog($"取消任务 {jobId} 失败：{exception.Message}", isError: true);
        }
    }

    private async Task CancelAllBackendTasksAsync()
    {
        var jobIds = _backendTaskItems
            .Where(item => item.CanCancel)
            .Select(item => item.Id)
            .ToArray();
        foreach (var jobId in jobIds)
        {
            await CancelBackendTaskAsync(jobId);
        }
    }

    private bool HandleBackendJob(BackendJob job, string successMessage)
    {
        if (_isClosing)
        {
            return false;
        }
        if (job.IsSuccessful)
        {
            StatusTextBlock.Text = successMessage;
            return true;
        }
        if (job.IsPartial)
        {
            var failed = Math.Max(job.FailureCount, job.Progress?.Failed ?? 0);
            StatusTextBlock.Text = $"{successMessage}，部分图片处理失败";
            AppendLog(
                $"任务 {job.Id} 已部分完成；失败图片 {failed} 张，可在“状态与诊断”查看。",
                isError: true
            );
            return true;
        }
        if (job.IsPaused || job.NeedsAttention)
        {
            StatusTextBlock.Text = job.IsPaused
                ? "任务已暂停；可在“状态与诊断”查看详情"
                : "任务需要处理；请查看“状态与诊断”";
            AppendLog($"任务 {job.Id} 当前状态：{job.Status}", isError: job.NeedsAttention);
            return false;
        }
        if (string.Equals(job.Status, "cancelled", StringComparison.OrdinalIgnoreCase))
        {
            StatusTextBlock.Text = "任务已取消；已完成的索引记录会保留";
            return false;
        }

        var message = job.Error?.Message ?? $"后端任务状态：{job.Status}";
        StatusTextBlock.Text = "常驻后端任务失败";
        AppendLog($"任务 {job.Id} 失败：{message}", isError: true);
        return false;
    }

    private async Task CancelCurrentOperationAsync()
    {
        _environmentOperationCancellation?.Cancel();
        if (_runner?.IsRunning == true)
        {
            await _runner.CancelCurrentAsync();
        }
    }

    private async Task WaitForCurrentOperationAsync(TimeSpan timeout)
    {
        var stopwatch = Stopwatch.StartNew();
        while (_isBusy && stopwatch.Elapsed < timeout)
        {
            await Task.Delay(100);
        }
    }

    private void SetBackendStatus(string text, BackendStatusKind kind)
    {
        BackendStatusTextBlock.Text = text;
        (BackendStatusBorder.Background, BackendStatusBorder.BorderBrush,
            BackendStatusTextBlock.Foreground) = kind switch
            {
                BackendStatusKind.Ready => (
                    new SolidColorBrush(Color.FromRgb(240, 253, 244)),
                    new SolidColorBrush(Color.FromRgb(187, 247, 208)),
                    new SolidColorBrush(Color.FromRgb(21, 128, 61))
                ),
                BackendStatusKind.Starting => (
                    new SolidColorBrush(Color.FromRgb(239, 246, 255)),
                    new SolidColorBrush(Color.FromRgb(191, 219, 254)),
                    new SolidColorBrush(Color.FromRgb(29, 78, 216))
                ),
                _ => (
                    new SolidColorBrush(Color.FromRgb(255, 247, 237)),
                    new SolidColorBrush(Color.FromRgb(254, 215, 170)),
                    new SolidColorBrush(Color.FromRgb(180, 83, 9))
                ),
            };
    }

    private static string PrettyJson(JsonElement element) => JsonSerializer.Serialize(
        element,
        new JsonSerializerOptions { WriteIndented = true }
    );

    private static void TryDeleteFile(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }

    private async Task<CommandResult?> RunCommandAsync(
        string runningStatus,
        string command,
        IEnumerable<string>? arguments = null,
        IReadOnlyDictionary<string, string?>? environment = null)
    {
        if (!EnsureRunner())
        {
            return null;
        }

        SetBusy(true, runningStatus);
        try
        {
            return await _runner!.RunZvecAsync(command, arguments, environment);
        }
        catch (Exception exception)
        {
            ShowOperationError("命令执行失败", exception);
            return null;
        }
        finally
        {
            SetBusy(false, StatusTextBlock.Text);
        }
    }

    private bool HandleCommandResult(
        CommandResult result,
        string successMessage,
        bool allowPartial)
    {
        if (result.WasCancelled)
        {
            StatusTextBlock.Text = "任务已取消；已完成的索引记录会保留";
            return false;
        }
        if (result.IsSuccess)
        {
            StatusTextBlock.Text = successMessage;
            return true;
        }
        if (allowPartial && result.IsPartialSuccess)
        {
            StatusTextBlock.Text = $"{successMessage}，部分文件处理失败";
            MessageBox.Show(
                this,
                "任务已完成，但有部分文件失败。请在运行日志和最终报告中查看详情。",
                "部分成功",
                MessageBoxButton.OK,
                MessageBoxImage.Warning
            );
            return true;
        }

        StatusTextBlock.Text = $"任务失败，退出码 {result.ExitCode}";
        var message = LastUsefulLine(result.StandardError)
            ?? LastUsefulLine(result.StandardOutput)
            ?? $"命令退出码：{result.ExitCode}";
        MessageBox.Show(
            this,
            message,
            "Zvec 命令执行失败",
            MessageBoxButton.OK,
            MessageBoxImage.Error
        );
        return false;
    }

    private async Task ReloadConfigurationAsync(bool loadLatestResults)
    {
        try
        {
            _configInspection = await _configService.InspectAsync();
            if (_configInspection.RequiresMigration)
            {
                _config = null;
                RefreshLibraryControls();
                ApplyLegacyConfigurationPresentation(_configInspection);
                _hasStoredApiKey = _apiCredentialService.HasApiKey();
                ApiKeyStatusTextBlock.Text = _hasStoredApiKey
                    ? "API Key 已保存到 Windows 凭据管理器"
                    : "API Key 尚未配置";
                if (IsLoaded)
                {
                    ApplyEnvironmentSnapshot();
                }
                return;
            }

            _config = await _configService.LoadAsync();
            RefreshLibraryControls(_config?.DefaultLibraryId);

            _hasStoredApiKey = _apiCredentialService.HasApiKey();
            ApiKeyStatusTextBlock.Text = _hasStoredApiKey
                ? "API Key 已保存到 Windows 凭据管理器"
                : "API Key 尚未配置";
            if (IsLoaded)
            {
                ApplyEnvironmentSnapshot();
            }

            if (loadLatestResults && _config is not null)
            {
                var session = await _resultLoader.LoadLatestAsync(_config);
                if (session is not null)
                {
                    ShowResultSession(session);
                }
            }
        }
        catch (Exception exception)
        {
            ShowOperationError("配置读取失败", exception);
        }
    }

    private void ApplyLegacyConfigurationPresentation(
        LauncherConfigInspection inspection)
    {
        if (!string.IsNullOrWhiteSpace(inspection.LibraryName))
        {
            SetupLibraryNameTextBox.Text = inspection.LibraryName;
        }
        if (!string.IsNullOrWhiteSpace(inspection.ImageRoot))
        {
            SetupImageRootTextBox.Text = inspection.ImageRoot;
        }
        if (!string.IsNullOrWhiteSpace(inspection.ResultsDirectory))
        {
            SetupResultsTextBox.Text = inspection.ResultsDirectory;
        }
        if (string.Equals(
            inspection.WorkspaceType,
            "bind",
            StringComparison.OrdinalIgnoreCase
        ) && !string.IsNullOrWhiteSpace(inspection.WorkspaceSource))
        {
            SetupWorkspaceTextBox.Text = inspection.WorkspaceSource;
        }

        _workspaceMigrationStatusOverride = inspection.HasNamedVolume
            ? inspection.NamedVolumes.FirstOrDefault() is { } volume
                ? $"旧版 Workspace：named volume “{volume.VolumeName}”将一次性导出到 {volume.DefaultExportDirectory}。"
                : "旧版 Workspace：检测到 Docker named volume。需一次性使用 Docker 导出、备份并迁移。"
            : "旧版 Workspace：需要先自动备份并迁移，已有向量会原样复用。";
        ConfigurationTextBlock.Text = inspection.HasNamedVolume
            ? "检测到旧版 Docker volume 图库，请先完成一次性迁移"
            : "检测到旧版图库配置，请先备份并迁移";
        LibraryEditorTitleText.Text = "旧版图库待迁移";
        SaveLibraryButton.Content = "请先备份与迁移";
        SaveLibraryButton.IsEnabled = false;
    }

    private void ShowResultSession(SearchResultSession session)
    {
        _currentResultSession = session;
        _resultPaging.ReplaceItems(session.Items);

        ResultsSummaryTextBlock.Text = session.Summary;
        ResultsEmptyTitleTextBlock.Text = session.EmptyTitle;
        ResultsEmptyCaptionTextBlock.Text = session.EmptyCaption;
        UpdateLowConfidenceButtonVisibility();
        RefreshResultPage();
    }

    private void PreviousResultsPage_Click(object sender, RoutedEventArgs e)
    {
        if (_resultPaging.MovePrevious())
        {
            RefreshResultPage();
        }
    }

    private void NextResultsPage_Click(object sender, RoutedEventArgs e)
    {
        if (_resultPaging.MoveNext())
        {
            RefreshResultPage();
        }
    }

    private void RefreshResultPage()
    {
        ResultsList.SelectedIndex = -1;
        _results.ReplaceAll(_resultPaging.CurrentPageItems);
        PreviousResultsPageButton.IsEnabled = _resultPaging.CanMovePrevious;
        NextResultsPageButton.IsEnabled = _resultPaging.CanMoveNext;
        ResultsPaginationPanel.Visibility = _resultPaging.TotalPageCount > 1
            ? Visibility.Visible
            : Visibility.Collapsed;
        ResultsPageTextBlock.Text = _resultPaging.TotalPageCount == 0
            ? "无结果"
            : $"第 {_resultPaging.CurrentPageNumber} / {_resultPaging.TotalPageCount} 页" +
                $" · 共 {_resultPaging.TotalItemCount} 张";

        if (_results.Count > 0)
        {
            ResultsList.SelectedIndex = 0;
            ResultsList.ScrollIntoView(_results[0]);
            return;
        }

        SelectedPreviewImage.Source = null;
        CancelSelectedResultPreview();
        SelectedNameText.Text = "没有匹配结果";
        SelectedScoreText.Text = string.Empty;
        SelectedDiagnosticScoreText.Text = string.Empty;
        SelectedLibraryText.Text = string.Empty;
        SelectedTagsText.Text = string.Empty;
        SelectedPathText.Text = string.Empty;
    }

    private void ShowLowConfidenceResults_Click(object sender, RoutedEventArgs e)
    {
        ShowLowConfidenceCheckBox.IsChecked = true;
        Search_Click(sender, e);
    }

    private void UpdateLowConfidenceCapability(bool supported)
    {
        _supportsLowConfidenceOverride = supported;
        ShowLowConfidenceCheckBox.IsEnabled = supported;
        if (!supported)
        {
            ShowLowConfidenceCheckBox.IsChecked = false;
        }
        ShowLowConfidenceCheckBox.ToolTip = supported
            ? "跳过相关性阈值和分数断层裁剪，仅用于诊断；这些结果不能视为可靠匹配。"
            : "当前常驻后端不支持该功能；更新桌面应用后可用。";
        UpdateLowConfidenceButtonVisibility();
    }

    private void UpdateTagOnlySearchCapability(bool supported)
    {
        SemanticTextSearchCheckBox.ToolTip = supported
            ? "关闭后只匹配图库中已有的标签，不调用大模型；支持跨图库统一排序。"
            : "关闭后单图库仍可本地按标签搜索；跨图库需要新版常驻后端。";
    }

    private void UpdateBackendSearchCapabilities(BackendHostService backend)
    {
        UpdateLowConfidenceCapability(backend.SupportsLowConfidenceOverride);
        UpdateTagOnlySearchCapability(backend.SupportsTagOnlySearch);
    }

    private void UpdateLowConfidenceButtonVisibility()
    {
        ResultsShowLowConfidenceButton.Visibility =
            _currentResultSession?.CanOfferLowConfidence(_supportsLowConfidenceOverride) == true
            ? Visibility.Visible
            : Visibility.Collapsed;
    }

    private void Runner_OutputReceived(object? sender, CommandOutputEventArgs e)
    {
        // Output can arrive much faster than the UI can lay out a TextBox. Queue the
        // text immediately and dispatch only the latest meaningful progress snapshot.
        AppendLog(e.Line, e.IsError);
        RunnerProgressUpdate? update = null;
        var match = EmbeddingProgressPattern.Match(e.Line);
        if (match.Success &&
            int.TryParse(match.Groups[1].Value, out var current) &&
            int.TryParse(match.Groups[2].Value, out var total) &&
            total > 0)
        {
            update = new RunnerProgressUpdate(
                false,
                total,
                Math.Min(current, total),
                $"正在生成图片向量：{current}/{total}"
            );
        }
        else if (e.Line.StartsWith("Scanning:", StringComparison.OrdinalIgnoreCase))
        {
            update = new RunnerProgressUpdate(true, 1, 0, "正在扫描图片目录…");
        }
        else if (e.Line.Contains("Optimizing", StringComparison.OrdinalIgnoreCase))
        {
            update = new RunnerProgressUpdate(true, 1, 0, "正在优化 Zvec 索引…");
        }

        if (update is not null)
        {
            QueueRunnerProgress(update);
        }
    }

    private void QueueRunnerProgress(RunnerProgressUpdate update)
    {
        Interlocked.Exchange(ref _pendingRunnerProgress, update);
        if (Interlocked.CompareExchange(ref _runnerProgressDispatchScheduled, 1, 0) == 0)
        {
            _ = Dispatcher.BeginInvoke(
                new Action(FlushPendingRunnerProgress),
                DispatcherPriority.Background
            );
        }
    }

    private void FlushPendingRunnerProgress()
    {
        var update = Interlocked.Exchange(ref _pendingRunnerProgress, null);
        if (update is not null)
        {
            BusyProgressBar.IsIndeterminate = update.IsIndeterminate;
            if (!update.IsIndeterminate)
            {
                BusyProgressBar.Minimum = 0;
                BusyProgressBar.Maximum = update.Maximum;
                BusyProgressBar.Value = update.Value;
            }
            StatusTextBlock.Text = update.Status;
        }

        Interlocked.Exchange(ref _runnerProgressDispatchScheduled, 0);
        if (Volatile.Read(ref _pendingRunnerProgress) is not null)
        {
            QueueRunnerProgress(Volatile.Read(ref _pendingRunnerProgress)!);
        }
    }

    private void SetBusy(bool busy, string status)
    {
        _isBusy = busy;
        var interaction = DesktopBusyInteractionState.Resolve(busy);
        MainTabControl.IsEnabled = interaction.TabControlEnabled;
        LibrarySettingsTabItem.IsEnabled = interaction.BusinessTabsEnabled;
        IndexTabItem.IsEnabled = interaction.BusinessTabsEnabled;
        SmartTagsTabItem.IsEnabled = interaction.BusinessTabsEnabled;
        SearchTabItem.IsEnabled = interaction.BusinessTabsEnabled;
        ModelConfigurationTabItem.IsEnabled = interaction.BusinessTabsEnabled;
        StatusDiagnosticsTabItem.IsEnabled = interaction.DiagnosticsTabEnabled;
        HeaderActionsPanel.IsEnabled = !busy;
        CancelButton.IsEnabled = interaction.ForegroundCancelEnabled;
        if (busy)
        {
            TaskLogExpander.IsExpanded = true;
        }
        BusyProgressBar.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        BusyProgressBar.IsIndeterminate = busy;
        BusyProgressBar.Value = 0;
        StatusTextBlock.Text = status;
    }

    private void AppendLog(string line, bool isError)
    {
        Interlocked.Increment(ref _pendingLogLineCount);
        _pendingLogLines.Enqueue((line, isError));
        while (Volatile.Read(ref _pendingLogLineCount) > 5_000 &&
               _pendingLogLines.TryDequeue(out _))
        {
            Interlocked.Decrement(ref _pendingLogLineCount);
            Interlocked.Increment(ref _droppedLogLineCount);
        }
    }

    private void FlushPendingLogLines()
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(FlushPendingLogLines));
            return;
        }
        // The collapsed log has no visual value, so retain its bounded queue without
        // paying TextBox document/layout costs until the user opens it.
        if (!LogTextBox.IsVisible)
        {
            return;
        }

        var buffer = new StringBuilder();
        var appended = 0;
        var timestamp = DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture);
        var dropped = Interlocked.Exchange(ref _droppedLogLineCount, 0);
        if (dropped > 0)
        {
            buffer.Append('[')
                .Append(timestamp)
                .Append("] 已省略 ")
                .Append(dropped)
                .AppendLine(" 条过早日志；后台任务未受影响。");
        }
        // Bound each UI tick by both line count and text size. Remaining entries stay
        // queued for the next tick, keeping input and window painting responsive.
        while (appended < 300 && buffer.Length < 64 * 1024 &&
               _pendingLogLines.TryDequeue(out var entry))
        {
            Interlocked.Decrement(ref _pendingLogLineCount);
            var prefix = entry.IsError ? "错误 " : string.Empty;
            buffer.Append('[')
                .Append(timestamp)
                .Append("] ")
                .Append(prefix)
                .Append(entry.Line)
                .AppendLine();
            appended++;
        }
        if (buffer.Length == 0)
        {
            return;
        }
        if (_renderedLogCharacterCount > 250_000)
        {
            var currentText = LogTextBox.Text;
            var keepFrom = Math.Max(0, currentText.Length - 150_000);
            var nextLine = currentText.IndexOf('\n', keepFrom);
            LogTextBox.Text = nextLine >= 0 ? currentText[(nextLine + 1)..] : currentText[keepFrom..];
            _renderedLogCharacterCount = LogTextBox.Text.Length;
        }
        var text = buffer.ToString();
        LogTextBox.AppendText(text);
        _renderedLogCharacterCount += text.Length;
        LogTextBox.ScrollToEnd();
    }

    private bool EnsureRunner()
    {
        if (_runner is not null)
        {
            return true;
        }

        ShowValidation(_startupError ?? "PowerShell 启动器不可用。");
        return false;
    }

    private static List<string> SplitTokens(string value) => value
        .Split(
            [' ', '\t', '\r', '\n', ',', ';', '，', '；'],
            StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries
        )
        .Distinct(StringComparer.Ordinal)
        .ToList();

    private static string GetSelectedComboValue(ComboBox comboBox, string fallback)
    {
        if (comboBox.SelectedItem is ModelChoiceItem model)
        {
            return model.Id;
        }
        if (comboBox.SelectedItem is ComboBoxItem item)
        {
            return item.Tag?.ToString() ?? item.Content?.ToString() ?? fallback;
        }

        return comboBox.SelectedValue?.ToString() ?? fallback;
    }

    private static bool TryParseInteger(string text, int min, int max, out int value) =>
        int.TryParse(text, NumberStyles.Integer, CultureInfo.CurrentCulture, out value) &&
        value >= min &&
        value <= max;

    private static bool TryParseDouble(string text, out double value) =>
        double.TryParse(text, NumberStyles.Float, CultureInfo.CurrentCulture, out value) ||
        double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out value);

    private void BrowseFolder(TextBox target, string title)
    {
        var dialog = new OpenFolderDialog
        {
            Title = title,
            Multiselect = false,
        };
        if (Directory.Exists(target.Text))
        {
            dialog.InitialDirectory = target.Text;
        }
        if (dialog.ShowDialog(this) == true)
        {
            target.Text = dialog.FolderName;
        }
    }

    private void OpenSelectedResult()
    {
        if (ResultsList.SelectedItem is not SearchResultItem item)
        {
            return;
        }

        var path = File.Exists(item.OriginalPath) ? item.OriginalPath : item.CopiedPath;
        if (!string.IsNullOrWhiteSpace(path))
        {
            OpenPath(path);
        }
    }

    private void OpenPath(string path)
    {
        try
        {
            if (!File.Exists(path) && !Directory.Exists(path))
            {
                throw new FileNotFoundException("路径已经不存在。", path);
            }
            Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
        }
        catch (Exception exception)
        {
            ShowOperationError("无法打开路径", exception);
        }
    }

    private static string? LastUsefulLine(string value) => value
        .Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
        .LastOrDefault();

    private void ShowValidation(string message) => MessageBox.Show(
        this,
        message,
        "请检查输入",
        MessageBoxButton.OK,
        MessageBoxImage.Information
    );

    private void ShowOperationError(string title, Exception exception)
    {
        StatusTextBlock.Text = title;
        AppendLog(exception.ToString(), isError: true);
        MessageBox.Show(
            this,
            exception.Message,
            title,
            MessageBoxButton.OK,
            MessageBoxImage.Error
        );
    }
}

internal readonly record struct DesktopBusyInteractionState(
    bool TabControlEnabled,
    bool BusinessTabsEnabled,
    bool DiagnosticsTabEnabled,
    bool ForegroundCancelEnabled)
{
    public static DesktopBusyInteractionState Resolve(bool busy) => new(
        TabControlEnabled: true,
        BusinessTabsEnabled: !busy,
        DiagnosticsTabEnabled: true,
        ForegroundCancelEnabled: busy
    );
}

internal static class TaskCenterPresentationPolicy
{
    public static bool ShouldExpand(IEnumerable<BackendTaskItem> tasks)
    {
        ArgumentNullException.ThrowIfNull(tasks);
        return tasks.Any(task => task.IsActive || task.HasFailures || task.NeedsAttention);
    }
}

internal sealed record RunnerProgressUpdate(
    bool IsIndeterminate,
    double Maximum,
    double Value,
    string Status
);

internal sealed record BackendJobBatchUpdate(
    IReadOnlyList<BackendJob> Jobs,
    bool IsRecovery,
    int AddedCount,
    int ActiveCount,
    int TotalCount
);

/// <summary>
/// Maintains one bounded, sequential job-list polling loop for the entire window.
/// It intentionally has no cancel-job dependency: cancelling its token stops only
/// local observation and can never send a backend DELETE request.
/// </summary>
internal sealed class BackendJobBatchMonitor
{
    private readonly Func<CancellationToken, Task<BackendJobListResponse>> _loadJobs;
    private readonly Func<BackendJobBatchUpdate, CancellationToken, Task> _applyUpdate;
    private readonly Func<Exception, int, CancellationToken, Task>? _reportError;
    private readonly TimeSpan _refreshInterval;
    private readonly HashSet<string> _knownJobIds = new(StringComparer.Ordinal);
    private readonly Dictionary<string, BackendJobPresentationState> _deliveredStates = new(
        StringComparer.Ordinal
    );
    private bool _hasDeliveredSnapshot;
    private int? _lastActiveCount;
    private int? _lastTotalCount;

    public BackendJobBatchMonitor(
        Func<CancellationToken, Task<BackendJobListResponse>> loadJobs,
        Func<BackendJobBatchUpdate, CancellationToken, Task> applyUpdate,
        TimeSpan refreshInterval,
        Func<Exception, int, CancellationToken, Task>? reportError = null)
    {
        _loadJobs = loadJobs ?? throw new ArgumentNullException(nameof(loadJobs));
        _applyUpdate = applyUpdate ?? throw new ArgumentNullException(nameof(applyUpdate));
        if (refreshInterval <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(
                nameof(refreshInterval),
                "The backend job refresh interval must be positive."
            );
        }
        _refreshInterval = refreshInterval;
        _reportError = reportError;
    }

    public async Task RefreshOnceAsync(
        bool isRecovery,
        CancellationToken cancellationToken = default)
    {
        var response = await _loadJobs(cancellationToken);
        cancellationToken.ThrowIfCancellationRequested();
        var jobs = DeduplicateByJobId(response.Jobs);
        var effectiveRecovery = isRecovery || !_hasDeliveredSnapshot;
        var currentIds = jobs
            .Select(job => job.Id)
            .ToHashSet(StringComparer.Ordinal);
        var addedIds = currentIds
            .Where(jobId => !_knownJobIds.Contains(jobId))
            .ToArray();
        var currentStates = jobs.ToDictionary(
            job => job.Id,
            job => BackendJobPresentationState.From(job),
            StringComparer.Ordinal
        );
        var changedJobs = effectiveRecovery
            ? jobs
            : jobs.Where(job =>
                !_deliveredStates.TryGetValue(job.Id, out var previous) ||
                previous != currentStates[job.Id]
            ).ToList();
        var activeCount = jobs.Count(job => !job.IsTerminal && !job.NeedsAttention);
        var update = new BackendJobBatchUpdate(
            changedJobs,
            effectiveRecovery,
            addedIds.Length,
            activeCount,
            response.TotalCount
        );

        // A healthy idle backend often returns byte-for-byte equivalent job objects.
        // Do not marshal those snapshots to the Dispatcher: visible rows already hold
        // the same state, and active/total counts have not changed either.
        if (effectiveRecovery || changedJobs.Count > 0 ||
            _lastActiveCount != activeCount || _lastTotalCount != response.TotalCount)
        {
            await _applyUpdate(update, cancellationToken);
        }
        // GetJobsAsync is bounded to the latest 50 rows. Retain only that moving
        // window so a desktop session that runs for days does not grow memory per job.
        _knownJobIds.Clear();
        _knownJobIds.UnionWith(currentIds);
        _deliveredStates.Clear();
        foreach (var entry in currentStates)
        {
            _deliveredStates.Add(entry.Key, entry.Value);
        }
        _lastActiveCount = activeCount;
        _lastTotalCount = response.TotalCount;
        _hasDeliveredSnapshot = true;
    }

    public async Task RunAsync(CancellationToken cancellationToken = default)
    {
        var consecutiveFailures = 0;
        while (true)
        {
            await Task.Delay(_refreshInterval, cancellationToken);
            try
            {
                await RefreshOnceAsync(isRecovery: false, cancellationToken);
                consecutiveFailures = 0;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception exception)
            {
                consecutiveFailures++;
                if (_reportError is not null)
                {
                    try
                    {
                        await _reportError(
                            exception,
                            consecutiveFailures,
                            cancellationToken
                        );
                    }
                    catch (OperationCanceledException) when (
                        cancellationToken.IsCancellationRequested
                    )
                    {
                        throw;
                    }
                    catch
                    {
                        // Diagnostics must not terminate task recovery.
                    }
                }
            }
        }
    }

    private static IReadOnlyList<BackendJob> DeduplicateByJobId(
        IEnumerable<BackendJob> jobs)
    {
        var unique = new List<BackendJob>();
        var positions = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var job in jobs)
        {
            if (string.IsNullOrWhiteSpace(job.Id))
            {
                continue;
            }
            if (positions.TryGetValue(job.Id, out var position))
            {
                // Prefer the last occurrence because it is normally the newest
                // snapshot when a backend response is assembled concurrently.
                unique[position] = job;
                continue;
            }
            positions.Add(job.Id, unique.Count);
            unique.Add(job);
        }
        return unique;
    }
}
