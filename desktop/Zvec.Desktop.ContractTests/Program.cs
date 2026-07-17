using System.Net;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Xml.Linq;
using Zvec.Desktop;
using Zvec.Desktop.Controls;
using Zvec.Desktop.Models;
using Zvec.Desktop.Services;

if (args.Length > 0)
{
    if (args.Length == 1 && string.Equals(
        args[0],
        "--backend-smoke",
        StringComparison.Ordinal
    ))
    {
        await RunBackendSmokeAsync();
        return;
    }
    if (args.Length == 1 && string.Equals(
        args[0],
        "--native-host-smoke",
        StringComparison.Ordinal
    ))
    {
        await RunNativeHostSmokeAsync();
        return;
    }
    if (args.Length == 2 && string.Equals(
        args[0],
        "--native-crash-helper",
        StringComparison.Ordinal
    ))
    {
        await RunNativeCrashHelperAsync(args[1]);
        return;
    }

    throw new ArgumentException(
        "Usage: Zvec.Desktop.ContractTests [--backend-smoke|--native-host-smoke]"
    );
}

await TestConfigMigrationAsync();
await TestRuntimeBootstrapContractsAsync();
await TestWorkspaceMigrationServiceAsync();
TestFirstUseInitializationEnvironment();
TestWorkspaceMigrationConfirmationPresentation();
TestFirstUseEnvironmentPresentation();
TestCredentialManager();
TestLegacyCredentialMigration();
await TestBackendInstanceRegistryContractsAsync();
await TestBackendLaunchLockCancellationAsync();
await TestBackendConfigurationFingerprintContractsAsync();
TestBackendSessionTokenStoreContracts();
await TestLiveBackendWithoutTokenIsPreservedAsync();
await PersistentBackendContract.RunAsync();
TestBackendCapabilityCompatibility();
TestBackendSearchRequestCompatibility();
TestAutoTagResultContracts();
TestAutoTagReviewWorkbenchContracts();
TestBackendTaskReadOnlyBindings();
TestTaskCenterDiagnosticsLayout();
TestSearchPagingAndTagModeLayout();
TestBusyInteractionState();
TestTaskCenterPresentationPolicy();
TestDesktopClosePolicy();
SearchResultPagingContract.Run();
TestAdaptiveResultGalleryLayout();
await QueryImageInputContract.RunAsync();
await TestAutoTagPagingContractsAsync();
TestBackendPublishPayload();
await TestSearchResultQualityPresentationAsync();
await TestJsonRequestsHaveContentLengthAsync();
await TestBackendJobListContractsAsync();
await TestBackendJobBatchMonitorAsync();
await TestAutoTagReviewWorkbenchApiContractsAsync();
await TestBackendDegradedHealthDiagnosticsAsync();
await TestBackendProtocolValidationAsync();
await TestBackendLifecycleCancellationAsync();
await TestBackendStartupCancellationStopsNativeProcessAsync();
await TestBackendCommandCancellationTerminatesProcessAsync();
await TestPowerShellStartupCancellationTerminatesProcessAsync();
Console.WriteLine("Desktop contract tests passed.");

static void TestDesktopClosePolicy()
{
    var policy = new DesktopClosePolicy();
    Assert(
        policy.Resolve(applicationCloseAllowed: false) ==
            DesktopCloseAction.HideToTray,
        "ordinary window close hides to tray"
    );
    policy.RequestExit();
    Assert(
        policy.Resolve(applicationCloseAllowed: false) ==
            DesktopCloseAction.BeginApplicationExit,
        "explicit exit enters the application shutdown path"
    );
    policy.CancelExit();
    Assert(
        policy.Resolve(applicationCloseAllowed: false) ==
            DesktopCloseAction.HideToTray,
        "cancelled exit restores close-to-tray behavior"
    );
    policy.RequestExit();
    Assert(
        policy.Resolve(applicationCloseAllowed: true) ==
            DesktopCloseAction.CompleteApplicationExit,
        "approved shutdown allows the final window close"
    );
}

static async Task TestBackendDegradedHealthDiagnosticsAsync()
{
    const string healthJson =
        """
        {
          "status": "degraded",
          "service_ready": false,
          "worker_alive": false,
          "error": {
            "code": "library_initialization_failed",
            "message": "One or more library services failed to initialize.",
            "details": {
              "cosplay-library": {
                "code": "service_initialization_failed",
                "message": "Unable to open the collection workspace.",
                "details": {
                  "type": "RuntimeError",
                  "library_id": "cosplay-library"
                }
              }
            }
          }
        }
        """;
    using var handler = new StatusJsonHandler(HttpStatusCode.ServiceUnavailable, healthJson);
    using var httpClient = new HttpClient(handler);
    using var client = new BackendApiClient(
        new Uri("http://127.0.0.1:8765/"),
        "contract-token",
        httpClient
    );

    var health = await client.GetHealthAsync();
    Assert(!health.IsReady, "degraded health is not ready");
    Assert(
        health.Error?.Code == "library_initialization_failed",
        "degraded health preserves the top-level error code"
    );
    var diagnostic = BackendHostService.FormatHealthDescription(health);
    Assert(
        diagnostic.Contains("error.code=library_initialization_failed", StringComparison.Ordinal),
        "startup diagnostic includes the health error code"
    );
    Assert(
        diagnostic.Contains(
            "error.message=One or more library services failed to initialize.",
            StringComparison.Ordinal
        ),
        "startup diagnostic includes the health error message"
    );
    Assert(
        diagnostic.Contains(
            "library[cosplay-library](code=service_initialization_failed",
            StringComparison.Ordinal
        ),
        "startup diagnostic identifies the failed library"
    );
    Assert(
        diagnostic.Contains(
            "message=Unable to open the collection workspace.",
            StringComparison.Ordinal
        ),
        "startup diagnostic includes the library startup error"
    );
    Assert(
        diagnostic.Contains("\"type\":\"RuntimeError\"", StringComparison.Ordinal) &&
            diagnostic.Contains("\"library_id\":\"cosplay-library\"", StringComparison.Ordinal),
        "startup diagnostic includes nested library error details"
    );
}

static void TestBackendTaskReadOnlyBindings()
{
    var mainWindowPath = FindRepositoryFile("desktop", "Zvec.Desktop", "MainWindow.xaml");
    var document = XDocument.Load(mainWindowPath, LoadOptions.SetLineInfo);
    var bindingAttributes = document
        .Descendants()
        .SelectMany(element => element.Attributes())
        .Where(attribute => attribute.Value.StartsWith("{Binding", StringComparison.Ordinal))
        .ToArray();
    var progressValueBindings = bindingAttributes
        .Where(attribute =>
            attribute.Name.LocalName == "Value" &&
            string.Equals(
                GetBindingOption(attribute.Value, "Path"),
                nameof(BackendTaskItem.ProgressValue),
                StringComparison.Ordinal
            )
        )
        .ToArray();

    Assert(
        progressValueBindings.Length == 1,
        "task progress has exactly one ProgressValue binding"
    );
    Assert(
        string.Equals(
            GetBindingOption(progressValueBindings[0].Value, "Mode"),
            "OneWay",
            StringComparison.Ordinal
        ),
        "read-only BackendTaskItem.ProgressValue binding is explicitly OneWay"
    );

    var taskTemplate = progressValueBindings[0]
        .Parent?
        .AncestorsAndSelf()
        .FirstOrDefault(element => element.Name.LocalName == "DataTemplate");
    Assert(taskTemplate is not null, "task progress binding belongs to a DataTemplate");

    var readOnlyProperties = typeof(BackendTaskItem)
        .GetProperties()
        .Where(property => property.CanRead && property.SetMethod is null)
        .Select(property => property.Name)
        .ToHashSet(StringComparer.Ordinal);
    var invalidBindings = taskTemplate!
        .Descendants()
        .SelectMany(element => element.Attributes())
        .Where(attribute => attribute.Value.StartsWith("{Binding", StringComparison.Ordinal))
        .Select(attribute => new
        {
            Attribute = attribute,
            Path = GetBindingOption(attribute.Value, "Path"),
            Mode = GetBindingOption(attribute.Value, "Mode"),
        })
        .Where(binding =>
            binding.Path is not null &&
            readOnlyProperties.Contains(binding.Path) &&
            (string.Equals(binding.Mode, "TwoWay", StringComparison.Ordinal) ||
                string.Equals(binding.Mode, "OneWayToSource", StringComparison.Ordinal))
        )
        .Select(binding =>
            $"{binding.Attribute.Parent?.Name.LocalName}.{binding.Attribute.Name.LocalName} " +
            $"<- {binding.Path} ({binding.Mode})"
        )
        .ToArray();

    Assert(
        invalidBindings.Length == 0,
        $"read-only BackendTaskItem bindings reject source-writing modes: {string.Join(", ", invalidBindings)}"
    );
}

static void TestTaskCenterDiagnosticsLayout()
{
    var mainWindowPath = FindRepositoryFile("desktop", "Zvec.Desktop", "MainWindow.xaml");
    var document = XDocument.Load(mainWindowPath, LoadOptions.SetLineInfo);
    var xNamespace = XNamespace.Get("http://schemas.microsoft.com/winfx/2006/xaml");

    XElement RequireNamedElement(string name)
    {
        var matches = document
            .Descendants()
            .Where(element =>
                string.Equals(
                    element.Attribute(xNamespace + "Name")?.Value,
                    name,
                    StringComparison.Ordinal
                )
            )
            .ToArray();
        Assert(matches.Length == 1, $"{name} appears exactly once in MainWindow.xaml");
        return matches[0];
    }

    var statusTab = RequireNamedElement("StatusDiagnosticsTabItem");
    Assert(
        statusTab.Name.LocalName == "TabItem" &&
            string.Equals(
                statusTab.Attribute("Header")?.Value,
                "⑤ 状态与诊断",
                StringComparison.Ordinal
            ),
        "status-and-diagnostics tab has a stable name and header"
    );

    foreach (var controlName in new[]
    {
        "TaskLogExpander",
        "TaskCenterSummaryTextBlock",
        "BackendTaskList",
        "RuntimeLogExpander",
        "LogTextBox",
        "CancelButton",
    })
    {
        Assert(
            RequireNamedElement(controlName).Ancestors().Contains(statusTab),
            $"{controlName} is hosted by the status-and-diagnostics tab"
        );
    }

    var rootGrid = document.Root!
        .Elements()
        .Single(element => element.Name.LocalName == "Grid");
    var rootRows = rootGrid
        .Elements()
        .Single(element => element.Name.LocalName == "Grid.RowDefinitions")
        .Elements()
        .Where(element => element.Name.LocalName == "RowDefinition")
        .ToArray();
    Assert(rootRows.Length == 4, "root layout no longer reserves a persistent task-center row");

    var statusBar = RequireNamedElement("BusyProgressBar").Parent;
    Assert(
        statusBar is not null &&
            ReferenceEquals(statusBar.Parent, rootGrid) &&
            string.Equals(statusBar.Attribute("Grid.Row")?.Value, "3", StringComparison.Ordinal),
        "global operation status occupies the final root row"
    );

    var taskList = RequireNamedElement("BackendTaskList");
    Assert(
        string.Equals(
            taskList.Attribute("VirtualizingPanel.VirtualizationMode")?.Value,
            "Recycling",
            StringComparison.Ordinal
        ) &&
            string.Equals(taskList.Attribute("MaxHeight")?.Value, "260", StringComparison.Ordinal),
        "diagnostics task list stays bounded and uses recycling virtualization"
    );
}

static void TestSearchPagingAndTagModeLayout()
{
    var mainWindowPath = FindRepositoryFile("desktop", "Zvec.Desktop", "MainWindow.xaml");
    var mainWindowCodePath = FindRepositoryFile(
        "desktop",
        "Zvec.Desktop",
        "MainWindow.xaml.cs"
    );
    var document = XDocument.Load(mainWindowPath, LoadOptions.SetLineInfo);
    var mainWindowCode = File.ReadAllText(mainWindowCodePath);
    var xNamespace = XNamespace.Get("http://schemas.microsoft.com/winfx/2006/xaml");

    XElement RequireNamedElement(string name)
    {
        var matches = document
            .Descendants()
            .Where(element =>
                string.Equals(
                    element.Attribute(xNamespace + "Name")?.Value,
                    name,
                    StringComparison.Ordinal
                )
            )
            .ToArray();
        Assert(matches.Length == 1, $"{name} appears exactly once in MainWindow.xaml");
        return matches[0];
    }

    var paginationPanel = RequireNamedElement("ResultsPaginationPanel");
    Assert(
        string.Equals(
            paginationPanel.Attribute("Grid.Row")?.Value,
            "2",
            StringComparison.Ordinal
        ),
        "result pagination is placed below the gallery"
    );
    Assert(
        string.Equals(
            RequireNamedElement("PreviousResultsPageButton").Attribute("Click")?.Value,
            "PreviousResultsPage_Click",
            StringComparison.Ordinal
        ) &&
            string.Equals(
                RequireNamedElement("NextResultsPageButton").Attribute("Click")?.Value,
                "NextResultsPage_Click",
                StringComparison.Ordinal
            ),
        "result paging buttons are wired to navigation handlers"
    );

    var resultPanel = paginationPanel
        .Parent!
        .Descendants()
        .Single(element => element.Name.LocalName == "VirtualizingWrapPanel");
    Assert(
        string.Equals(
            resultPanel.Attribute("StretchItemsToViewport")?.Value,
            "True",
            StringComparison.Ordinal
        ),
        "paged result cards fill the available gallery viewport"
    );
    Assert(
        string.Equals(
            resultPanel.Attribute("ExactFillItemCount")?.Value,
            "15",
            StringComparison.Ordinal
        ),
        "a full 15-item page uses a gap-free gallery grid"
    );
    Assert(
        string.Equals(
            RequireNamedElement("TopKTextBox").Attribute("Text")?.Value,
            "15",
            StringComparison.Ordinal
        ),
        "search defaults to 15 returned images"
    );
    Assert(
        Regex.IsMatch(
            mainWindowCode,
            @"private\s+const\s+int\s+SearchResultPageSize\s*=\s*15\s*;",
            RegexOptions.CultureInvariant
        ),
        "the desktop result gallery displays 15 items per page"
    );

    var metadataBackfillButton = RequireNamedElement("MetadataBackfillButton");
    Assert(
        string.Equals(
            metadataBackfillButton.Attribute("Content")?.Value,
            "生成描述向量",
            StringComparison.Ordinal
        ) &&
            string.Equals(
                metadataBackfillButton.Attribute("Click")?.Value,
                "MetadataBackfill_Click",
                StringComparison.Ordinal
            ),
        "metadata embedding backfill button is visible and wired to its handler"
    );

    var queryDropZone = RequireNamedElement("QueryImageDropZone");
    Assert(
        string.Equals(queryDropZone.Attribute("AllowDrop")?.Value, "True", StringComparison.Ordinal) &&
            string.Equals(
                queryDropZone.Attribute("Drop")?.Value,
                "QueryImageDropZone_Drop",
                StringComparison.Ordinal
            ) &&
            string.Equals(
                queryDropZone.Attribute("PreviewKeyDown")?.Value,
                "QueryImageDropZone_PreviewKeyDown",
                StringComparison.Ordinal
            ),
        "query-image surface accepts drag/drop and Ctrl+V"
    );
    foreach (var (name, handler) in new[]
    {
        ("ChooseQueryImageButton", "BrowseQueryImage_Click"),
        ("PasteQueryImageButton", "PasteQueryImage_Click"),
        ("BrowseQueryImageDirectoryButton", "BrowseQueryImageDirectory_Click"),
        ("ClearQueryImageButton", "ClearQueryImage_Click"),
    })
    {
        Assert(
            string.Equals(
                RequireNamedElement(name).Attribute("Click")?.Value,
                handler,
                StringComparison.Ordinal
            ),
            $"{name} is wired to {handler}"
        );
    }
    Assert(
        string.Equals(
            RequireNamedElement("SelectedPreviewImage").Attribute("Stretch")?.Value,
            "UniformToFill",
            StringComparison.Ordinal
        ),
        "selected image preview fills and crops its viewport"
    );

    var semanticToggle = RequireNamedElement("SemanticTextSearchCheckBox");
    Assert(
        string.Equals(semanticToggle.Attribute("IsChecked")?.Value, "True", StringComparison.Ordinal),
        "semantic text search remains enabled by default"
    );
    Assert(
        string.Equals(
            semanticToggle.Attribute("Checked")?.Value,
            "SemanticTextSearchCheckBox_Changed",
            StringComparison.Ordinal
        ) &&
            string.Equals(
                semanticToggle.Attribute("Unchecked")?.Value,
                "SemanticTextSearchCheckBox_Changed",
                StringComparison.Ordinal
        ),
        "semantic search toggle refreshes the text-search presentation"
    );

    var diversityToggle = RequireNamedElement("DiversifyResultsCheckBox");
    Assert(
        string.Equals(
            diversityToggle.Attribute("IsChecked")?.Value,
            "True",
            StringComparison.Ordinal
        ),
        "series diversity is enabled by default"
    );
}

static void TestAdaptiveResultGalleryLayout()
{
    var filled = VirtualizingWrapPanel.CalculateStretchedLayout(
        itemCount: 15,
        viewportWidth: 1096,
        viewportHeight: 796,
        minimumItemWidth: 160,
        minimumItemHeight: 236,
        requireExactFill: true
    );
    Assert(filled is not null, "a regular gallery viewport supports exact fill");
    Assert(
        filled.Value.Columns == 5 && filled.Value.Rows == 3,
        "15 results fill the regular gallery as a gap-free 5 by 3 grid"
    );
    Assert(
        filled.Value.Columns * filled.Value.Rows == 15,
        "the regular gallery does not reserve empty card slots"
    );

    var compact = VirtualizingWrapPanel.CalculateStretchedLayout(
        itemCount: 15,
        viewportWidth: 496,
        viewportHeight: 796,
        minimumItemWidth: 160,
        minimumItemHeight: 236,
        requireExactFill: true
    );
    Assert(
        compact is null,
        "a compact gallery preserves minimum card size and falls back to scrolling"
    );
}

static void TestBusyInteractionState()
{
    var busy = DesktopBusyInteractionState.Resolve(busy: true);
    Assert(busy.TabControlEnabled, "busy state keeps the tab control interactive");
    Assert(!busy.BusinessTabsEnabled, "busy state disables business tabs");
    Assert(busy.DiagnosticsTabEnabled, "busy state keeps diagnostics available");
    Assert(busy.ForegroundCancelEnabled, "busy state enables foreground cancellation");

    var idle = DesktopBusyInteractionState.Resolve(busy: false);
    Assert(
        idle.TabControlEnabled &&
            idle.BusinessTabsEnabled &&
            idle.DiagnosticsTabEnabled &&
            !idle.ForegroundCancelEnabled,
        "idle state restores business tabs and disables foreground cancellation"
    );
}

static void TestTaskCenterPresentationPolicy()
{
    Assert(
        !TaskCenterPresentationPolicy.ShouldExpand([]),
        "empty task history stays collapsed"
    );

    var completed = new BackendTaskItem();
    completed.Update(new BackendJob
    {
        Id = "task-completed",
        Command = "index",
        Status = "succeeded",
    });
    Assert(
        !TaskCenterPresentationPolicy.ShouldExpand([completed]),
        "successful task history does not permanently expand diagnostics"
    );

    var running = new BackendTaskItem();
    running.Update(new BackendJob
    {
        Id = "task-running",
        Command = "auto_tag",
        Status = "running",
    });
    Assert(
        TaskCenterPresentationPolicy.ShouldExpand([running]),
        "active task automatically expands diagnostics details"
    );

    var partial = new BackendTaskItem();
    partial.Update(new BackendJob
    {
        Id = "task-partial",
        Command = "index",
        Status = "partial",
        FailureCount = 2,
    });
    Assert(
        TaskCenterPresentationPolicy.ShouldExpand([partial]),
        "task with failures automatically expands diagnostics details"
    );
}

static string? GetBindingOption(string expression, string optionName)
{
    if (!expression.StartsWith("{Binding", StringComparison.Ordinal))
    {
        return null;
    }

    var body = expression["{Binding".Length..].Trim().TrimEnd('}').Trim();
    var namedOption = Regex.Match(
        body,
        $@"(?:^|,)\s*{Regex.Escape(optionName)}\s*=\s*([^,}}]+)",
        RegexOptions.CultureInvariant
    );
    if (namedOption.Success)
    {
        return namedOption.Groups[1].Value.Trim();
    }

    if (!string.Equals(optionName, "Path", StringComparison.Ordinal))
    {
        return null;
    }

    var positionalPath = body.Split(',', 2, StringSplitOptions.TrimEntries)[0];
    return positionalPath.Length > 0 && !positionalPath.Contains('=')
        ? positionalPath
        : null;
}

static string FindRepositoryFile(params string[] relativeSegments)
{
    var searchRoots = new[] { Directory.GetCurrentDirectory(), AppContext.BaseDirectory };
    foreach (var searchRoot in searchRoots)
    {
        for (var directory = new DirectoryInfo(searchRoot); directory is not null; directory = directory.Parent)
        {
            var candidate = Path.Combine(
                new[] { directory.FullName }.Concat(relativeSegments).ToArray()
            );
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }
    }

    throw new FileNotFoundException(
        $"Could not locate repository file: {Path.Combine(relativeSegments)}"
    );
}

static void TestFirstUseInitializationEnvironment()
{
    var automatic = FirstUseInitializationEnvironment.Create(null);
    Assert(
        automatic.TryGetValue("DASHSCOPE_API_KEY", out var clearedKey) &&
            clearedKey is null,
        "first-use init clears inherited API key"
    );
    Assert(
        !automatic.ContainsKey("ZVEC_PYTHON"),
        "first-use init leaves automatic Python selection unset"
    );

    var custom = FirstUseInitializationEnvironment.Create(
        @"  C:\Portable Python\python.exe  "
    );
    Assert(
        custom.TryGetValue("ZVEC_PYTHON", out var configuredPython) &&
            configuredPython == @"C:\Portable Python\python.exe",
        "first-use init forwards the advanced Python override"
    );
    Assert(
        custom.TryGetValue("DASHSCOPE_API_KEY", out clearedKey) &&
            clearedKey is null,
        "custom Python init still clears inherited API key"
    );
}

static void TestWorkspaceMigrationConfirmationPresentation()
{
    var plan = new WorkspaceBackupPlan
    {
        Status = "ready",
        BackupMode = "metadata",
        Destination = @"C:\backup",
        EstimatedPayloadBytes = 1024,
    };
    var bindDetail = WorkspaceMigrationConfirmationFormatter.Build(plan, []);
    Assert(
        bindDetail.Contains("预计数据量：1 KB", StringComparison.Ordinal),
        "bind migration shows the backup estimate"
    );
    Assert(
        !bindDetail.Contains("导出体积：未知", StringComparison.Ordinal),
        "bind migration has no named-volume warning"
    );

    var namedVolumeDetail = WorkspaceMigrationConfirmationFormatter.Build(
        plan,
        [
            new LauncherNamedVolumeInspection
            {
                VolumeName = "legacy-zvec-volume",
                DefaultExportDirectory = @"D:\Zvec\exported-workspace",
            },
        ]
    );
    Assert(
        namedVolumeDetail.Contains("元数据备份估算：1 KB", StringComparison.Ordinal),
        "named-volume migration labels the metadata-only estimate"
    );
    Assert(
        namedVolumeDetail.Contains(
            "不包含 named volume 导出",
            StringComparison.Ordinal
        ),
        "named-volume estimate excludes export payload"
    );
    Assert(
        namedVolumeDetail.Contains("导出体积：未知", StringComparison.Ordinal),
        "named-volume export size is explicitly unknown"
    );
    Assert(
        namedVolumeDetail.Contains(
            @"legacy-zvec-volume → D:\Zvec\exported-workspace",
            StringComparison.Ordinal
        ),
        "named-volume destination is shown"
    );
}

static async Task TestWorkspaceMigrationServiceAsync()
{
    var backupJson = """
        bootstrap completed
        {
          "status":"planned",
          "plan":{
            "status":"ready",
            "backup_mode":"metadata",
            "destination":"C:/backup",
            "estimated_payload_bytes":1024,
            "required_free_bytes":2048,
            "available_free_bytes":4096,
            "destination_writable":true,
            "libraries":[],
            "blockers":[],
            "warnings":[],
            "api_requests":0
          },
          "api_requests":0
        }
        """;
    var runner = new FakeWorkspaceCommandRunner(
        new CommandResult(0, backupJson, string.Empty, false)
    );
    var service = new WorkspaceMigrationService(runner);
    var response = await service.PlanBackupAsync(
        new WorkspaceBackupRequest("library-main", "C:/backup", FullBackup: true)
    );
    Assert(response.IsSuccess, "workspace backup plan command succeeds");
    Assert(response.Report.Plan?.CanProceed == true, "workspace backup plan is usable");
    Assert(response.Report.Plan?.ApiRequests == 0, "workspace backup plan uses no API");
    Assert(runner.LastCommand == "workspace-backup", "workspace backup command name");
    Assert(
        runner.LastArguments.SequenceEqual(
            [
                "--library",
                "library-main",
                "--destination",
                "C:/backup",
                "--full",
                "--dry-run",
            ]
        ),
        "workspace backup arguments"
    );

    var migrationJson = """
        {
          "status":"dry_run",
          "config_schema":3,
          "workspace_backup":{"status":"planned","api_requests":0},
          "exports":[
            {"status":"planned","volume":"legacy-volume","destination":"C:/export"}
          ],
          "verification":[],
          "api_requests":0
        }
        """;
    runner.Result = new CommandResult(0, migrationJson, string.Empty, false);
    var migration = await service.PlanMigrationAsync(
        new WorkspaceMigrationRequest(
            "library-main",
            "C:/export",
            "C:/backup",
            FullBackup: false
        )
    );
    Assert(migration.Report.RequiresNamedVolumeExport, "named-volume plan is explicit");
    Assert(
        migration.Report.EffectiveWorkspaceBackup?.Status == "planned",
        "legacy migration exposes workspace backup uniformly"
    );
    Assert(migration.Report.ApiRequests == 0, "workspace migration uses no API");
    Assert(runner.LastCommand == "migrate-docker-workspace", "migration command name");

    var nativeMigrationJson = """
        {
          "status":"dry_run",
          "backup":{
            "status":"planned",
            "plan":{
              "status":"ready",
              "backup_mode":"metadata",
              "destination":"C:/backup",
              "estimated_payload_bytes":1024,
              "required_free_bytes":2048,
              "available_free_bytes":4096,
              "destination_writable":true,
              "libraries":[],
              "blockers":[],
              "warnings":[],
              "api_requests":0
            },
            "api_requests":0
          },
          "api_requests":0
        }
        """;
    runner.Result = new CommandResult(0, nativeMigrationJson, string.Empty, false);
    var nativeMigration = await service.PlanMigrationAsync(
        new WorkspaceMigrationRequest("library-main")
    );
    Assert(
        nativeMigration.Report.EffectiveWorkspaceBackup?.Plan?.CanProceed == true,
        "schema v3 migration exposes workspace backup uniformly"
    );
}

static void TestFirstUseEnvironmentPresentation()
{
    var firstLaunch = FirstUseEnvironmentEvaluator.Evaluate(
        new FirstUseEnvironmentInput
        {
            RunnerAvailable = true,
            PythonReady = false,
            DependenciesReady = false,
            ApiKeyConfigured = false,
            ImageRoot = string.Empty,
            WorkspaceDirectory = @"C:\Zvec\workspace",
            ResultsDirectory = @"C:\Zvec\results",
        }
    );
    Assert(
        firstLaunch.Python.State == EnvironmentCheckState.ActionRequired,
        "first-use Python action"
    );
    Assert(
        firstLaunch.Dependencies.State == EnvironmentCheckState.ActionRequired,
        "first-use dependency action"
    );
    Assert(
        firstLaunch.NextAction.Contains("自动修复运行环境", StringComparison.Ordinal),
        "first-use repair action"
    );
    Assert(!firstLaunch.CanInitialize, "first-use initialization remains gated");

    var ready = FirstUseEnvironmentEvaluator.Evaluate(
        new FirstUseEnvironmentInput
        {
            RunnerAvailable = true,
            PythonReady = true,
            PythonDetail = "Python 3.12 · x64",
            DependenciesReady = true,
            DependenciesDetail = "zvec 0.5.1 · Pillow 12.3.0",
            ApiKeyConfigured = true,
            ImageRoot = @"C:\Pictures",
            ImageRootExists = true,
            ImageRootReadable = true,
            WorkspaceDirectory = @"C:\Zvec\workspace",
            WorkspaceExists = false,
            WorkspaceWritable = true,
            ResultsDirectory = @"C:\Zvec\results",
            ResultsDirectoryExists = false,
            ResultsDirectoryWritable = true,
        }
    );
    Assert(ready.Python.State == EnvironmentCheckState.Ready, "ready Python state");
    Assert(
        ready.Workspace.State == EnvironmentCheckState.Pending,
        "workspace auto-create state"
    );
    Assert(
        ready.ResultsDirectory.State == EnvironmentCheckState.Pending,
        "results auto-create state"
    );
    Assert(ready.CanInitialize, "ready first-use initialization");
    Assert(
        ready.NextAction.Contains("保存图库配置", StringComparison.Ordinal),
        "ready first-use next action"
    );

    var inaccessible = FirstUseEnvironmentEvaluator.Evaluate(
        new FirstUseEnvironmentInput
        {
            RunnerAvailable = true,
            PythonReady = true,
            DependenciesReady = true,
            ApiKeyConfigured = true,
            ImageRoot = @"C:\Pictures",
            ImageRootExists = true,
            ImageRootReadable = false,
            WorkspaceDirectory = @"C:\Zvec\workspace",
            WorkspaceWritable = true,
            ResultsDirectory = @"C:\Zvec\results",
            ResultsDirectoryWritable = true,
        }
    );
    Assert(
        inaccessible.ImageRoot.State == EnvironmentCheckState.ActionRequired,
        "unreadable image root state"
    );
    Assert(!inaccessible.CanInitialize, "unreadable image root blocks initialization");

    var overlap = FirstUseEnvironmentEvaluator.Evaluate(
        new FirstUseEnvironmentInput
        {
            RunnerAvailable = true,
            PythonReady = true,
            DependenciesReady = true,
            ApiKeyConfigured = true,
            ImageRoot = @"C:\Pictures",
            ImageRootExists = true,
            ImageRootReadable = true,
            WorkspaceDirectory = @"C:\Pictures\workspace",
            WorkspaceWritable = true,
            ResultsDirectory = @"C:\Zvec\results",
            ResultsDirectoryWritable = true,
            PathLayoutError = "Workspace 不能位于图片目录内。",
        }
    );
    Assert(
        overlap.Workspace.State == EnvironmentCheckState.ActionRequired,
        "overlapping workspace state"
    );
    Assert(
        overlap.ResultsDirectory.State == EnvironmentCheckState.ActionRequired,
        "overlapping results state"
    );
    Assert(
        overlap.NextAction.Contains("修正目录设置", StringComparison.Ordinal),
        "overlapping path next action"
    );

    var probeRoot = Path.Combine(
        Path.GetTempPath(),
        $"zvec-first-use-probe-{Guid.NewGuid():N}"
    );
    try
    {
        Directory.CreateDirectory(probeRoot);
        var blockedComponent = Path.Combine(probeRoot, "component-is-a-file");
        File.WriteAllText(blockedComponent, "not a directory");
        Assert(
            !FirstUsePathProbe.IsWritableDirectoryOrParent(
                Path.Combine(blockedComponent, "workspace")
            ),
            "file path component blocks workspace creation"
        );
        Assert(
            FirstUsePathProbe.IsWritableDirectoryOrParent(
                Path.Combine(probeRoot, "new-workspace")
            ),
            "writable parent permits workspace creation"
        );
        Assert(
            Directory.GetFiles(probeRoot, ".zvec-write-probe-*.tmp").Length == 0,
            "write probe leaves no temporary file"
        );
    }
    finally
    {
        Directory.Delete(probeRoot, recursive: true);
    }
}

static async Task RunNativeHostSmokeAsync()
{
    var originalConfigHome = Environment.GetEnvironmentVariable("ZVEC_CONFIG_HOME");
    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-native-host-smoke-{Guid.NewGuid():N}"
    );
    var credential = new CredentialManagerService(
        $"Zvec.ImageSearch/NativeHostSmoke/{Guid.NewGuid():N}"
    );
    try
    {
        var configHome = Directory.CreateDirectory(Path.Combine(root, "config-home")).FullName;
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", configHome);
        var images = Directory.CreateDirectory(Path.Combine(root, "images")).FullName;
        var workspace = Directory.CreateDirectory(Path.Combine(root, "workspace")).FullName;
        var results = Directory.CreateDirectory(Path.Combine(root, "results")).FullName;
        var entryPoint = Environment.GetEnvironmentVariable("ZVEC_NATIVE_SMOKE_ENTRYPOINT");
        var python = Environment.GetEnvironmentVariable("ZVEC_NATIVE_SMOKE_PYTHON") ??
            (OperatingSystem.IsWindows() ? "python.exe" : "python3");
        var config = new LauncherConfig
        {
            SchemaVersion = LauncherConfig.CurrentSchemaVersion,
            PythonExecutable = python,
            ResultsDirectory = results,
            DefaultLibraryId = "library-smoke",
            Libraries =
            [
                new LauncherLibrary
                {
                    Id = "library-smoke",
                    Name = "Native smoke",
                    ImageRoot = images,
                    WorkspaceDirectory = workspace,
                    Enabled = true,
                },
            ],
        };
        var credentialService = new ApiCredentialService(
            credential,
            new LauncherConfigService()
        );
        var options = new BackendHostOptions
        {
            AutoPrepareRuntime = false,
            BackendEntryPointPath = entryPoint,
            ApplicationDirectory = AppContext.BaseDirectory,
            StartupTimeout = TimeSpan.FromSeconds(30),
        };
        string stagingDirectory;
        int originalPort;
        await using (var firstBackend = new BackendHostService(
            config,
            options: options,
            credentialService: credentialService
        ))
        {
            var client = await firstBackend.StartAsync();
            var health = await client.GetHealthAsync();
            Assert(health.IsReady, "native host health");
            var version = await client.GetVersionAsync();
            Assert(version.ProtocolVersion == 2, "native host protocol");

            var query = Path.Combine(root, "query.jpg");
            await File.WriteAllBytesAsync(query, [0xff, 0xd8, 0xff, 0xd9]);
            stagingDirectory = firstBackend.QueryStagingDirectory;
            originalPort = firstBackend.HostPort;
            var staged = await firstBackend.StageQueryFileAsync(query);
            Assert(File.Exists(staged.BackendPath), "native query staging");
            Assert(staged.BackendPath == staged.HostPath, "native query path is host path");
            await firstBackend.DetachAsync();
            Assert(!firstBackend.IsRunning, "detached native host releases the first client");
        }

        await using (var secondBackend = new BackendHostService(
            config,
            options: options,
            credentialService: credentialService
        ))
        {
            var reattachedClient = await secondBackend.StartAsync();
            Assert(
                secondBackend.ConnectionMode == BackendConnectionMode.Attached,
                "second native host reattaches instead of spawning"
            );
            Assert(secondBackend.HostPort == originalPort, "reattached native host keeps endpoint");
            Assert((await reattachedClient.GetHealthAsync()).IsReady, "reattached health");
            await secondBackend.StopAsync();
            Assert(!secondBackend.IsRunning, "reattached native host stopped");
        }
        Assert(!Directory.Exists(stagingDirectory), "owned query staging cleaned");
        await new LauncherConfigService().SaveAsync(config);
        await RunNativeCrashRecoverySmokeAsync(
            config,
            options,
            credentialService,
            root,
            configHome
        );
        Console.WriteLine(
            "Native backend smoke passed: detach and forced desktop termination both " +
            "reattached to the original backend, then stopped cleanly."
        );
    }
    finally
    {
        credential.DeleteApiKey();
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", originalConfigHome);
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static async Task RunNativeCrashHelperAsync(string markerPath)
{
    ArgumentException.ThrowIfNullOrWhiteSpace(markerPath);
    var configService = new LauncherConfigService();
    var config = await configService.LoadAsync() ?? throw new InvalidOperationException(
        "Native crash helper could not load its launcher configuration."
    );
    var entryPoint = Environment.GetEnvironmentVariable("ZVEC_NATIVE_SMOKE_ENTRYPOINT");
    var credentialService = new ApiCredentialService(
        new CredentialManagerService(
            $"Zvec.ImageSearch/NativeCrashHelper/{Environment.ProcessId}"
        ),
        configService
    );
    await using var backend = new BackendHostService(
        config,
        options: new BackendHostOptions
        {
            AutoPrepareRuntime = false,
            BackendEntryPointPath = entryPoint,
            ApplicationDirectory = AppContext.BaseDirectory,
            StartupTimeout = TimeSpan.FromSeconds(30),
        },
        credentialService: credentialService
    );
    var client = await backend.StartAsync();
    var health = await client.GetHealthAsync();
    if (!health.IsReady)
    {
        throw new InvalidOperationException("Native crash helper backend is not ready.");
    }

    var fullMarkerPath = Path.GetFullPath(markerPath);
    var markerDirectory = Path.GetDirectoryName(fullMarkerPath) ?? throw new InvalidOperationException(
        "Native crash helper marker needs a parent directory."
    );
    Directory.CreateDirectory(markerDirectory);
    var temporaryMarker = Path.Combine(
        markerDirectory,
        $".{Path.GetFileName(fullMarkerPath)}.{Guid.NewGuid():N}.tmp"
    );
    var marker = JsonSerializer.SerializeToUtf8Bytes(new
    {
        instance_id = backend.InstanceId,
        port = backend.HostPort,
        query_root = backend.QueryStagingDirectory,
    });
    await File.WriteAllBytesAsync(temporaryMarker, marker);
    File.Move(temporaryMarker, fullMarkerPath, overwrite: true);
    Console.WriteLine("Native crash helper is ready for forced termination.");
    await Task.Delay(Timeout.InfiniteTimeSpan);
}

static async Task RunNativeCrashRecoverySmokeAsync(
    LauncherConfig config,
    BackendHostOptions options,
    ApiCredentialService credentialService,
    string root,
    string configHome)
{
    var markerPath = Path.Combine(root, "native-crash-ready.json");
    var startInfo = CreateContractTestChildStartInfo(
        "--native-crash-helper",
        markerPath
    );
    startInfo.Environment["ZVEC_CONFIG_HOME"] = configHome;
    var child = new Process { StartInfo = startInfo };
    var childStarted = false;
    var markerObserved = false;
    var backendStopped = false;
    var standardOutput = Task.FromResult(string.Empty);
    var standardError = Task.FromResult(string.Empty);
    try
    {
        if (!child.Start())
        {
            throw new InvalidOperationException("Could not start native crash helper.");
        }
        childStarted = true;
        standardOutput = child.StandardOutput.ReadToEndAsync();
        standardError = child.StandardError.ReadToEndAsync();
        var startup = Stopwatch.StartNew();
        while (startup.Elapsed < TimeSpan.FromSeconds(45))
        {
            if (File.Exists(markerPath))
            {
                markerObserved = true;
                break;
            }
            if (child.HasExited)
            {
                throw new InvalidOperationException(
                    "Native crash helper exited before its backend was ready.\n" +
                    $"stdout: {await standardOutput}\n" +
                    $"stderr: {await standardError}"
                );
            }
            await Task.Delay(100);
        }
        if (!markerObserved)
        {
            throw new TimeoutException("Native crash helper did not become ready in time.");
        }

        using var marker = JsonDocument.Parse(await File.ReadAllBytesAsync(markerPath));
        var originalInstanceId = marker.RootElement.GetProperty("instance_id").GetString() ??
            throw new InvalidDataException("Native crash marker omitted instance_id.");
        var originalPort = marker.RootElement.GetProperty("port").GetInt32();
        var queryRoot = marker.RootElement.GetProperty("query_root").GetString() ??
            throw new InvalidDataException("Native crash marker omitted query_root.");

        // Kill only the desktop helper. Killing the process tree would also kill the
        // Python backend and would not exercise abnormal-window recovery.
        child.Kill(entireProcessTree: false);
        await child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
        await Task.Delay(750);

        await using var recovered = new BackendHostService(
            config,
            options: options,
            credentialService: credentialService
        );
        var client = await recovered.StartAsync();
        Assert(
            recovered.ConnectionMode == BackendConnectionMode.Attached,
            "forced desktop termination must reattach instead of spawning a backend"
        );
        Assert(
            recovered.InstanceId == originalInstanceId && recovered.HostPort == originalPort,
            "forced desktop termination must preserve the original backend identity"
        );
        Assert(
            recovered.QueryStagingDirectory == queryRoot,
            "forced desktop termination must preserve the original query staging root"
        );
        Assert((await client.GetHealthAsync()).IsReady, "crash-recovered backend health");
        await recovered.StopAsync();
        backendStopped = true;
        Assert(!Directory.Exists(queryRoot), "crash-recovered query staging cleaned");
    }
    finally
    {
        if (childStarted && !child.HasExited)
        {
            child.Kill(entireProcessTree: !markerObserved);
            await child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
        }
        child.Dispose();

        if (!backendStopped && markerObserved)
        {
            try
            {
                await using var cleanup = new BackendHostService(
                    config,
                    options: options,
                    credentialService: credentialService
                );
                await cleanup.StartAsync();
                await cleanup.StopAsync();
            }
            catch
            {
                // Preserve the original smoke failure. The isolated temp config keeps
                // any remaining descriptor away from the user's real installation.
            }
        }
    }
}

static ProcessStartInfo CreateContractTestChildStartInfo(params string[] arguments)
{
    var processPath = Environment.ProcessPath ?? throw new InvalidOperationException(
        "Cannot resolve the current contract-test process."
    );
    var assemblyPath = System.Reflection.Assembly.GetExecutingAssembly().Location;
    var startInfo = new ProcessStartInfo
    {
        FileName = processPath,
        UseShellExecute = false,
        CreateNoWindow = true,
        RedirectStandardOutput = true,
        RedirectStandardError = true,
    };
    if (string.Equals(
        Path.GetFileNameWithoutExtension(processPath),
        "dotnet",
        StringComparison.OrdinalIgnoreCase
    ))
    {
        startInfo.ArgumentList.Add(assemblyPath);
    }
    foreach (var argument in arguments)
    {
        startInfo.ArgumentList.Add(argument);
    }
    return startInfo;
}

static async Task RunBackendSmokeAsync()
{
    var baseUrl = GetRequiredEnvironmentVariable("ZVEC_BACKEND_SMOKE_URL");
    var token = GetRequiredEnvironmentVariable("ZVEC_BACKEND_SMOKE_TOKEN");
    var apiKey = GetRequiredEnvironmentVariable("ZVEC_BACKEND_SMOKE_API_KEY");
    var libraryId = GetRequiredEnvironmentVariable("ZVEC_BACKEND_SMOKE_LIBRARY_ID");
    var apiUrl = Environment.GetEnvironmentVariable("ZVEC_BACKEND_SMOKE_API_URL") ??
        "https://example.invalid/embeddings";

    using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(60));
    using var client = new BackendApiClient(new Uri(baseUrl), token);
    var health = await client.GetHealthAsync(timeout.Token);
    Assert(health.IsReady, "real backend health");

    await client.ConfigureCredentialsAsync(apiKey, apiUrl, timeout.Token);
    var submitted = await client.SubmitJobAsync(
        "stats",
        new Dictionary<string, object?> { ["library_id"] = libraryId },
        timeout.Token
    );
    var completed = await client.PollJobAsync(
        submitted.Id,
        TimeSpan.FromMilliseconds(100),
        cancellationToken: timeout.Token
    );
    if (!completed.IsSuccessful)
    {
        throw new Exception(
            $"Real backend stats job ended as '{completed.Status}': " +
            $"{completed.Error?.Code} {completed.Error?.Message}"
        );
    }
    Assert(completed.Result.HasValue, "real backend stats result");
    Console.WriteLine(
        $"Real backend smoke passed: credentials configured; stats job {completed.Id} succeeded."
    );
}

static string GetRequiredEnvironmentVariable(string name)
{
    var value = Environment.GetEnvironmentVariable(name);
    return string.IsNullOrWhiteSpace(value)
        ? throw new InvalidOperationException($"Environment variable {name} is required.")
        : value;
}

static void TestAutoTagResultContracts()
{
    using var document = JsonDocument.Parse(
        """
        {
          "processed": 1,
          "cached": 0,
          "succeeded": 1,
          "failed": 0,
          "actual_cost_cny": 0.0012,
          "proposals": [
            {
              "proposal_id": "proposal-001",
              "relative_path": "原神-刻晴/001.jpg",
              "description": "紫发角色回头看镜头",
              "tags": ["旧自由标签"],
              "suggested_tags": ["紫发自由建议"],
              "proposed_tags": ["回头", "看镜头", "回头", ""],
              "folder_tags": ["原神-刻晴"],
              "fields": {
                "action": {
                  "values": ["action_looking_back"],
                  "labels": ["回头"],
                  "confidence": 0.88
                },
                "gaze": {
                  "values": ["gaze_at_camera"],
                  "labels": ["看镜头"],
                  "confidence": 0.92
                },
                "expression": {
                  "values": ["expression_serious"],
                  "labels": ["严肃"],
                  "confidence": 0.72
                }
              },
              "entities": {
                "real_person": [{
                  "name": null,
                  "state": "unable_to_confirm",
                  "evidence": ["visual"],
                  "confidence": 0.4
                }],
                "cosplayer": [{
                  "name": null,
                  "state": "not_applicable",
                  "evidence": ["none"],
                  "confidence": 1.0
                }],
                "character": [{
                    "name": "疑似其他角色",
                    "state": "suggested",
                    "evidence": ["visual"],
                    "confidence": 0.99
                  }, {
                    "name": "刻晴",
                    "state": "confirmed",
                    "evidence": ["folder_name", "visual"],
                    "confidence": 0.93
                }],
                "work": [{
                  "name": "原神",
                  "state": "suggested",
                  "evidence": ["visual"],
                  "confidence": 0.76
                }]
              },
              "review_required": true,
              "review_reasons": [
                "field:expression:low_confidence",
                "field:people_count:conflict",
                "entity:work:suggested",
                "entity:character:context_mismatch"
              ],
              "model_trace": ["qwen3-vl-flash", "qwen3-vl-plus"],
              "resolved_model": "qwen3-vl-plus",
              "escalated": true
            }
          ]
        }
        """
    );
    var job = new BackendJob
    {
        Id = "auto-tag-result-contract",
        Command = "auto_tag",
        Status = "succeeded",
        Result = document.RootElement.Clone(),
    };
    var result = job.DeserializeResult<AutoTagRunResult>();
    Assert(result.Processed == 1, "auto-tag result processed count");
    Assert(result.Proposals.Count == 1, "auto-tag result proposal count");
    var proposal = result.Proposals[0];
    Assert(proposal.StableId == "proposal-001", "auto-tag stable proposal ID");
    Assert(
        proposal.RecommendedTags.SequenceEqual(["回头", "看镜头"]),
        "only proposed_tags become reviewable tags"
    );
    Assert(!proposal.RecommendedTags.Contains("旧自由标签"), "legacy free tags not prefilled");
    Assert(!proposal.RecommendedTags.Contains("紫发自由建议"), "suggested tags not prefilled");
    Assert(!proposal.RecommendedTags.Contains("刻晴"), "desktop does not append confirmed entities");
    Assert(!proposal.RecommendedTags.Contains("原神"), "desktop does not append suggested entities");
    Assert(proposal.Entities.Character.Count == 2, "v2 entity arrays are preserved");
    Assert(
        proposal.EntitySummary.Contains("角色：刻晴（确认）", StringComparison.Ordinal),
        "entity summary prioritizes confirmed state"
    );
    Assert(
        proposal.EntitySummary.Contains("作品：原神（待确认）", StringComparison.Ordinal),
        "suggested entity state is localized"
    );
    Assert(
        proposal.EntitySummary.Contains("真人：无法确认", StringComparison.Ordinal),
        "unable-to-confirm entity state is localized"
    );
    Assert(
        proposal.StableFieldSummary.Contains("动作：回头", StringComparison.Ordinal),
        "stable field labels are presented in Chinese"
    );
    Assert(
        !proposal.StableFieldSummary.Contains("action_looking_back", StringComparison.Ordinal),
        "stable field codes stay out of the UI summary"
    );
    Assert(
        proposal.StableFieldConfidenceText.Contains("72", StringComparison.Ordinal),
        "minimum stable-field confidence is presented"
    );
    Assert(
        proposal.ModelPolicySummary.Contains("Flash → Plus", StringComparison.Ordinal) &&
        proposal.ModelPolicySummary.Contains("自动升级", StringComparison.Ordinal),
        "model trace and escalation are presented"
    );
    Assert(
        proposal.ReviewReasonsText.Contains("神态置信度较低", StringComparison.Ordinal) &&
        proposal.ReviewReasonsText.Contains("人数结果存在冲突", StringComparison.Ordinal) &&
        proposal.ReviewReasonsText.Contains("作品为模型推断", StringComparison.Ordinal) &&
        proposal.ReviewReasonsText.Contains(
            "角色依据与当前人工标签或目录不一致",
            StringComparison.Ordinal
        ),
        "review reasons are localized"
    );
    var reviewItem = AutoTagReviewItem.FromProposal(proposal);
    Assert(reviewItem.EditedTagsText == "回头 看镜头", "review draft uses proposed tags only");
    Assert(reviewItem.HasStableFields, "review item exposes stable field details");
    Assert(reviewItem.HasReviewReasons, "review item exposes review reasons");

    using var legacyDocument = JsonDocument.Parse(
        """
        {
          "processed": 1,
          "proposals": [{
            "proposal_id": "legacy-proposal",
            "relative_path": "legacy.jpg",
            "tags": ["旧标签"],
            "suggested_tags": ["旧自由建议"],
            "entities": {
              "character": {
                "name": "旧角色",
                "state": "confirmed",
                "evidence": ["folder_name"],
                "confidence": 0.9
              }
            }
          }]
        }
        """
    );
    var legacyJob = new BackendJob
    {
        Id = "legacy-auto-tag-result-contract",
        Command = "auto_tag",
        Status = "succeeded",
        Result = legacyDocument.RootElement.Clone(),
    };
    var legacyProposal = legacyJob.DeserializeResult<AutoTagRunResult>().Proposals[0];
    Assert(legacyProposal.Entities.Character.Count == 1, "legacy entity object is accepted");
    Assert(legacyProposal.RecommendedTags.Count == 0, "legacy free tags are not auto-prefilled");
    Assert(
        AutoTagReviewItem.FromProposal(legacyProposal).EditedTagsText.Length == 0,
        "legacy review draft starts empty without proposed_tags"
    );
}

static async Task TestAutoTagPagingContractsAsync()
{
    using var document = JsonDocument.Parse(
        """
        {
          "pending_count": 1981,
          "offset": 100,
          "limit": 100,
          "has_more": true,
          "proposals": [
            {
              "proposal_id": "proposal-101",
              "relative_path": "人物写真/0101.jpg",
              "description": "人物侧身站立",
              "tags": ["人物", "侧身"],
              "suggested_tags": ["人物"],
              "folder_tags": ["人物写真"],
              "entities": {}
            }
          ]
        }
        """
    );
    var job = new BackendJob
    {
        Id = "auto-tag-pending-contract",
        Command = "auto_tag_pending",
        Status = "succeeded",
        Result = document.RootElement.Clone(),
    };
    var result = await job.DeserializeResultAsync<AutoTagPendingResult>();
    Assert(result.PendingCount == 1981, "pending auto-tag total count");
    Assert(result.Offset == 100 && result.Limit == 100, "pending auto-tag page bounds");
    Assert(result.HasMore, "pending auto-tag next page state");
    Assert(result.Proposals.Count == 1, "pending auto-tag page proposals");
    var item = AutoTagReviewItem.FromProposal(result.Proposals[0]);
    Assert(item.ProposalId == "proposal-101", "pending proposal maps to review item");
    Assert(
        item.EditedTagsText.Length == 0,
        "legacy pending free tags are not automatically prefilled"
    );
    Assert(!item.HasUnsavedChanges, "new review item starts without a draft change");
    item.Decision = "accept";
    Assert(item.HasUnsavedChanges, "review decision marks the page item as changed");

    using var largeDocument = JsonDocument.Parse(
        JsonSerializer.Serialize(new
        {
            processed = 2000,
            cached = 1900,
            succeeded = 2000,
            failed = 0,
            stopped_reason = "",
            actual_cost_cny = 0.25m,
            proposals = Enumerable.Range(0, 2000).Select(index => new
            {
                proposal_id = $"proposal-{index}",
                relative_path = $"images/{index}.jpg",
            }),
        })
    );
    var largeJob = new BackendJob
    {
        Id = "auto-tag-large-summary-contract",
        Command = "auto_tag",
        Status = "succeeded",
        Result = largeDocument.RootElement.Clone(),
    };
    var summary = await largeJob.DeserializeResultAsync<AutoTagRunSummaryResult>();
    Assert(summary.Processed == 2000, "large auto-tag payload uses summary contract");
    Assert(summary.Cached == 1900, "large auto-tag summary cache count");
}

static void TestAutoTagReviewWorkbenchContracts()
{
    using var document = JsonDocument.Parse(
        """
        {
          "pending_count": 1,
          "offset": 0,
          "limit": 100,
          "has_more": false,
          "undo_available": true,
          "proposals": [{
            "proposal_id": "proposal-review-001",
            "relative_path": "原神/刻晴/001.jpg",
            "source_path": "D:/图库/原神/刻晴/001.jpg",
            "description": "角色站立写真",
            "existing_tags": ["写真"],
            "proposed_tags": ["站姿", "刻晴", "原神"],
            "low_risk_tags": ["站姿", "刻晴"],
            "identity_tags": ["刻晴", "原神"],
            "tag_details": [
              {
                "tag": "写真",
                "source": "manual",
                "risk": "none",
                "identity": false,
                "already_present": true
              },
              {
                "tag": "站姿",
                "source": "model_field",
                "sources": ["model_field"],
                "source_label": "模型字段",
                "risk": "low",
                "identity": false,
                "field": "pose",
                "already_present": false
              },
              {
                "tag": "刻晴",
                "source": "model_entity",
                "risk": "identity",
                "identity": true,
                "entity_type": "character",
                "already_present": false
              },
              {
                "tag": "原神",
                "source": "model_entity",
                "risk": "identity",
                "identity": true,
                "entity_type": "work",
                "already_present": false
              }
            ]
          }]
        }
        """
    );
    var job = new BackendJob
    {
        Id = "auto-tag-review-workbench-contract",
        Command = "auto_tag_pending",
        Status = "succeeded",
        Result = document.RootElement.Clone(),
    };
    var result = job.DeserializeResult<AutoTagPendingResult>();
    Assert(result.UndoAvailable, "pending review restores batch undo availability");
    var item = AutoTagReviewItem.FromProposal(result.Proposals[0]);
    Assert(item.SourcePath.EndsWith("001.jpg", StringComparison.Ordinal), "review source path mapped");
    Assert(item.ExistingTagsText == "写真", "existing tags are presented separately");
    Assert(item.EditedTagsText == "站姿", "identity tags are not placed in the ordinary draft");
    Assert(item.BatchSafeTags.SequenceEqual(["站姿"]), "batch tags contain low-risk fields only");
    Assert(item.IdentityChoices.Count == 2, "identity tags become individual confirmation choices");
    Assert(!item.HasUnsavedChanges, "review workbench item starts clean");
    item.IsBatchSelected = true;
    Assert(item.HasUnsavedChanges, "batch selection participates in discard protection");
    item.IsBatchSelected = false;
    Assert(!item.HasUnsavedChanges, "clearing batch selection restores clean state");
    item.IsDetailsExpanded = true;
    Assert(!item.HasUnsavedChanges, "diagnostic expansion is presentation state only");
    item.IsDetailsExpanded = false;
    Assert(item.TagDetails.Any(detail => detail.SourceText == "模型字段"), "model field source is localized");
    Assert(
        item.TagDetails.Where(detail => detail.Tag is "刻晴" or "原神").All(detail => detail.IsIdentityTag),
        "model entity source is treated as identity"
    );

    static IReadOnlyList<string> ParseTags(string value) => value
        .Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
        .ToList();

    item.EditedTagsText = "站姿 刻晴 原神";
    Assert(
        item.BuildAcceptedTags(ParseTags).SequenceEqual(["站姿"]),
        "unchecked identity tags cannot be submitted through ordinary text"
    );
    Assert(item.BuildConfirmedIdentityTags().Count == 0, "identity starts unconfirmed");
    item.IdentityChoices.Single(choice => choice.Tag == "刻晴").IsConfirmed = true;
    Assert(item.Decision == "accept", "confirming an identity marks the item for individual acceptance");
    Assert(
        item.BuildAcceptedTags(ParseTags).SequenceEqual(["站姿", "刻晴"]),
        "only explicitly checked identity is added to individual submission"
    );
    Assert(
        item.BuildConfirmedIdentityTags().SequenceEqual(["刻晴"]),
        "confirmed_identity_tags contains the checked identity only"
    );
    Assert(
        item.BuildBatchAcceptedTags().SequenceEqual(["站姿"]),
        "batch acceptance never includes checked or unchecked identity tags"
    );

    var filters = new AutoTagReviewFilters
    {
        LatestIndexOnly = true,
        Character = "刻晴",
        Work = "原神",
        Action = "站立",
        Expression = "微笑",
        ReviewState = "identity",
    };
    var pendingParameters = new AutoTagPendingRequest
    {
        LibraryId = "library-a",
        Offset = 0,
        Limit = 100,
        Filters = filters,
    }.ToParameters();
    var filterParameters = (IReadOnlyDictionary<string, object?>)pendingParameters["filters"]!;
    Assert((bool)filterParameters["latest_index_only"]!, "latest-index review filter contract");
    Assert((string)filterParameters["character"]! == "刻晴", "character review filter contract");
    Assert((string)filterParameters["work"]! == "原神", "work review filter contract");
    Assert((string)filterParameters["action"]! == "站立", "action review filter contract");
    Assert((string)filterParameters["expression"]! == "微笑", "expression review filter contract");
    Assert((string)filterParameters["review_state"]! == "identity", "review state filter contract");

    var batchParameters = new AutoTagBatchReviewRequest
    {
        LibraryId = "library-a",
        ProposalIds = [item.ProposalId],
        AcceptedTagsByProposal = new Dictionary<string, IReadOnlyList<string>>(StringComparer.Ordinal)
        {
            [item.ProposalId] = item.BuildBatchAcceptedTags(),
        },
    }.ToParameters();
    Assert((bool)batchParameters["exclude_identity_tags"]!, "batch request enforces identity exclusion");
    var batchTags = (IReadOnlyDictionary<string, IReadOnlyList<string>>)
        batchParameters["accepted_tags_by_proposal"]!;
    Assert(batchTags[item.ProposalId].SequenceEqual(["站姿"]), "batch request contains safe tags only");

    var hundredProposalIds = Enumerable.Range(1, 100)
        .Select(index => $"proposal-{index:000}")
        .ToList();
    var hundredBatchParameters = new AutoTagBatchReviewRequest
    {
        LibraryId = "library-a",
        ProposalIds = hundredProposalIds,
        AcceptedTagsByProposal = hundredProposalIds.ToDictionary(
            proposalId => proposalId,
            _ => (IReadOnlyList<string>)["站姿"],
            StringComparer.Ordinal
        ),
    }.ToParameters();
    Assert(
        ((IReadOnlyList<string>)hundredBatchParameters["proposal_ids"]!).Count == 100,
        "batch review carries one full 100-image workbench page"
    );
    Assert(
        ((IReadOnlyDictionary<string, IReadOnlyList<string>>)
            hundredBatchParameters["accepted_tags_by_proposal"]!).Count == 100,
        "batch review maps safe tags for all 100 selected images"
    );

    var landscapeDecode = AutoTagThumbnailSizing.GetDecodeDimensions(4000, 2000);
    var portraitDecode = AutoTagThumbnailSizing.GetDecodeDimensions(2000, 4000);
    Assert(
        landscapeDecode == (AutoTagThumbnailSizing.LongestEdge, 0),
        "landscape thumbnail constrains its longest edge"
    );
    Assert(
        portraitDecode == (0, AutoTagThumbnailSizing.LongestEdge),
        "portrait thumbnail constrains its longest edge"
    );

    var decisionJson = JsonSerializer.Serialize(new AutoTagReviewDecision
    {
        ProposalId = item.ProposalId,
        Decision = "accept",
        AcceptedTags = item.BuildAcceptedTags(ParseTags),
        ConfirmedIdentityTags = item.BuildConfirmedIdentityTags(),
    });
    using var decisionDocument = JsonDocument.Parse(decisionJson);
    Assert(
        decisionDocument.RootElement
            .GetProperty("confirmed_identity_tags")[0]
            .GetString() == "刻晴",
        "individual review serializes explicit identity confirmation"
    );

    var batchResult = JsonSerializer.Deserialize<AutoTagBatchReviewResult>(
        """
        {
          "batch_id":"batch-001",
          "accepted":1,
          "updated":1,
          "identity_excluded":2,
          "identity_excluded_count":2,
          "identity_exclusions":[{
            "proposal_id":"proposal-review-001",
            "tags":["刻晴","原神"]
          }],
          "undo_available":true
        }
        """
    ) ?? throw new Exception("Batch review result was not deserialized.");
    Assert(batchResult.EffectiveIdentityExcludedCount == 2, "batch identity exclusions mapped");
    Assert(batchResult.IdentityExclusions[0].Tags.Count == 2, "batch identity exclusion details mapped");
    Assert(batchResult.UndoAvailable, "batch result enables undo");

    var alias = JsonSerializer.Deserialize<TagAliasEntry>(
        """{"canonical":"雷电将军","aliases":["雷神","影"]}"""
    ) ?? throw new Exception("Alias compatibility payload was not deserialized.");
    Assert(alias.EffectiveCanonicalName == "雷电将军", "legacy alias canonical name is accepted");
    Assert(alias.DisplayText.Contains("雷神", StringComparison.Ordinal), "alias relation is presented");
}

static void TestBackendPublishPayload()
{
    foreach (var relativePath in new[]
    {
        Path.Combine("backend", "image_service.py"),
        Path.Combine("backend", "zvec_launcher.py"),
        Path.Combine("backend", "zvec_logging.py"),
        Path.Combine("backend", "pyproject.toml"),
        Path.Combine("backend", "requirements.txt"),
        Path.Combine("backend", "requirements-lock.txt"),
        Path.Combine("backend", "model-catalog.default.json"),
        Path.Combine("backend", "README.md"),
        Path.Combine("backend", "image_vector_service", "backend_server.py"),
        Path.Combine("backend", "image_vector_service", "backend_instance_lock.py"),
    })
    {
        Assert(
            File.Exists(Path.Combine(AppContext.BaseDirectory, relativePath)),
            $"desktop payload includes {relativePath}"
        );
    }
}

static async Task TestConfigMigrationAsync()
{
    var originalConfigHome = Environment.GetEnvironmentVariable("ZVEC_CONFIG_HOME");
    var originalHome = Environment.GetEnvironmentVariable("ZVEC_DOCKER_CONFIG_HOME");
    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-desktop-contract-{Guid.NewGuid():N}"
    );
    try
    {
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", root);
        Environment.SetEnvironmentVariable("ZVEC_DOCKER_CONFIG_HOME", root);
        Directory.CreateDirectory(root);
        var images = Directory.CreateDirectory(Path.Combine(root, "images")).FullName;
        var workspace = Directory.CreateDirectory(Path.Combine(root, "workspace")).FullName;
        var results = Directory.CreateDirectory(Path.Combine(root, "results")).FullName;
        var legacy = new
        {
            schema_version = 1,
            image_name = "zvec-image-search:test",
            image_root = images,
            workspace_type = "bind",
            workspace_source = workspace,
            results_directory = results,
        };
        await File.WriteAllTextAsync(
            Path.Combine(root, "config.json"),
            JsonSerializer.Serialize(legacy),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false)
        );

        var v1MigrationCalls = 0;
        var service = new LauncherConfigService(async cancellationToken =>
        {
            v1MigrationCalls++;
            await File.WriteAllTextAsync(
                Path.Combine(root, "config.json"),
                JsonSerializer.Serialize(new
                {
                    schema_version = 3,
                    results_directory = results,
                    default_library_id = "library-v1",
                    libraries = new[]
                    {
                        new
                        {
                            id = "library-v1",
                            name = "Migrated v1",
                            image_root = images,
                            workspace_directory = workspace,
                            enabled = true,
                        },
                    },
                }),
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
                cancellationToken
            );
        });
        var v1Inspection = await service.InspectAsync();
        Assert(v1Inspection.RequiresMigration, "v1 migration is detected read-only");
        Assert(!v1Inspection.DockerRequired, "v1 bind does not require Docker");
        Assert(v1Inspection.ApiRequests == 0, "v1 inspection uses no API");
        var migrated = await service.LoadAsync() ?? throw new Exception(
            "Migrated config was not loaded."
        );
        Assert(migrated.SchemaVersion == LauncherConfig.CurrentSchemaVersion, "schema");
        Assert(migrated.Libraries.Count == 1, "library count");
        Assert(migrated.DefaultLibrary?.ImageRoot == images, "image root");
        Assert(migrated.DefaultLibrary?.WorkspaceDirectory == workspace, "workspace directory");
        Assert(v1MigrationCalls == 1, "v1 migration delegates to native service");

        var persistedV3 = await File.ReadAllTextAsync(Path.Combine(root, "config.json"));
        Assert(persistedV3.Contains("workspace_directory", StringComparison.Ordinal), "v3 workspace field");
        Assert(!persistedV3.Contains("workspace_type", StringComparison.Ordinal), "v3 omits workspace type");
        Assert(!persistedV3.Contains("workspace_source", StringComparison.Ordinal), "v3 omits workspace source");
        Assert(!persistedV3.Contains("image_name", StringComparison.Ordinal), "v3 omits image name");

        var duplicate = new LauncherLibrary
        {
            Id = $"lib-{Guid.NewGuid():N}",
            Name = "Duplicate workspace",
            ImageRoot = images,
            WorkspaceDirectory = workspace,
        };
        migrated.Libraries.Add(duplicate);
        await AssertThrowsAsync<InvalidDataException>(() => service.SaveAsync(migrated));

        migrated.Libraries.Remove(duplicate);
        migrated.ResultsDirectory = images;
        await AssertThrowsAsync<InvalidDataException>(() => service.SaveAsync(migrated));

        var v2Root = Directory.CreateDirectory(Path.Combine(root, "v2")).FullName;
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", v2Root);
        var v2MigrationCalls = 0;
        var v2Service = new LauncherConfigService(async cancellationToken =>
        {
            v2MigrationCalls++;
            await File.WriteAllTextAsync(
                Path.Combine(v2Root, "config.json"),
                JsonSerializer.Serialize(new
                {
                    schema_version = 3,
                    results_directory = results,
                    default_library_id = "library-v2",
                    libraries = new[]
                    {
                        new
                        {
                            id = "library-v2",
                            name = "V2 bind",
                            image_root = images,
                            workspace_directory = workspace,
                            enabled = true,
                        },
                    },
                }),
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
                cancellationToken
            );
        });
        await File.WriteAllTextAsync(
            Path.Combine(v2Root, "config.json"),
            JsonSerializer.Serialize(new
            {
                schema_version = 2,
                image_name = "zvec-image-search:test",
                results_directory = results,
                default_library_id = "library-v2",
                libraries = new[]
                {
                    new
                    {
                        id = "library-v2",
                        name = "V2 bind",
                        image_root = images,
                        workspace_type = "bind",
                        workspace_source = workspace,
                        enabled = true,
                    },
                },
            }),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false)
        );
        var migratedV2 = await v2Service.LoadAsync() ?? throw new Exception(
            "Migrated v2 config was not loaded."
        );
        Assert(migratedV2.SchemaVersion == 3, "v2 migrates to schema v3");
        Assert(migratedV2.DefaultLibrary?.WorkspaceDirectory == workspace, "v2 bind reused");
        Assert(v2MigrationCalls == 1, "v2 migration delegates to native service");

        var volumeRoot = Directory.CreateDirectory(Path.Combine(root, "volume")).FullName;
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", volumeRoot);
        var volumeMigrationCalled = false;
        var volumeService = new LauncherConfigService(_ =>
        {
            volumeMigrationCalled = true;
            return Task.CompletedTask;
        });
        await File.WriteAllTextAsync(
            Path.Combine(volumeRoot, "config.json"),
            JsonSerializer.Serialize(new
            {
                schema_version = 2,
                image_name = "zvec-image-search:test",
                results_directory = results,
                default_library_id = "library-volume",
                libraries = new[]
                {
                    new
                    {
                        id = "library-volume",
                        name = "V2 volume",
                        image_root = images,
                        workspace_type = "volume",
                        workspace_source = "zvec_workspace",
                        enabled = true,
                    },
                },
            }),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false)
        );
        var volumeInspection = await volumeService.InspectAsync();
        Assert(volumeInspection.RequiresMigration, "named volume migration is detected");
        Assert(volumeInspection.DockerRequired, "named volume explicitly requires Docker");
        Assert(volumeInspection.NamedVolumes.Count == 1, "named volume details available");
        Assert(
            volumeInspection.NamedVolumes[0].VolumeName == "zvec_workspace",
            "named volume name is exposed"
        );
        Assert(
            volumeInspection.NamedVolumes[0].DefaultExportDirectory.Contains(
                "workspace",
                StringComparison.OrdinalIgnoreCase
            ),
            "named volume default export target is exposed"
        );
        Assert(volumeInspection.ApiRequests == 0, "named volume inspection uses no API");
        var volumeError = await CaptureExceptionAsync<InvalidDataException>(
            () => volumeService.LoadAsync()
        );
        Assert(volumeError.Message.Contains("named volume", StringComparison.Ordinal), "volume migration explains source");
        Assert(volumeError.Message.Contains("导出", StringComparison.Ordinal), "volume migration explains export");
        Assert(!volumeMigrationCalled, "named volume blocks before native migration");
    }
    finally
    {
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", originalConfigHome);
        Environment.SetEnvironmentVariable("ZVEC_DOCKER_CONFIG_HOME", originalHome);
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static void TestCredentialManager()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }
    var target = $"Zvec.ImageSearch/ContractTests/{Guid.NewGuid():N}";
    var credential = new CredentialManagerService(target);
    var value = $"test-key-{Guid.NewGuid():N}-中文";
    try
    {
        credential.SaveApiKey(value);
        Assert(credential.HasApiKey(), "credential exists");
        Assert(credential.ReadApiKey() == value, "credential round trip");
    }
    finally
    {
        credential.DeleteApiKey();
    }
    Assert(!credential.HasApiKey(), "credential cleanup");
}

static void TestLegacyCredentialMigration()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    var originalConfigHome = Environment.GetEnvironmentVariable("ZVEC_CONFIG_HOME");
    var originalHome = Environment.GetEnvironmentVariable("ZVEC_DOCKER_CONFIG_HOME");
    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-credential-migration-{Guid.NewGuid():N}"
    );
    var target = $"Zvec.ImageSearch/ContractTests/{Guid.NewGuid():N}";
    var credential = new CredentialManagerService(target);
    var key = $"legacy-key-{Guid.NewGuid():N}";
    try
    {
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", root);
        Environment.SetEnvironmentVariable("ZVEC_DOCKER_CONFIG_HOME", root);
        Directory.CreateDirectory(root);
        File.WriteAllText(
            Path.Combine(root, ".env"),
            $"DASHSCOPE_API_KEY={key}{Environment.NewLine}" +
                $"DASHSCOPE_API_URL=https://example.invalid/api{Environment.NewLine}",
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false)
        );

        var service = new ApiCredentialService(
            credential,
            new LauncherConfigService()
        );
        Assert(service.MigrateLegacyApiKey(), "legacy credential migrated");
        Assert(credential.ReadApiKey() == key, "migrated credential value");

        var remaining = File.ReadAllText(Path.Combine(root, ".env"));
        Assert(!remaining.Contains("DASHSCOPE_API_KEY=", StringComparison.Ordinal), "legacy key removed");
        Assert(remaining.Contains("DASHSCOPE_API_URL=", StringComparison.Ordinal), "legacy URL retained");
        Assert(!service.MigrateLegacyApiKey(), "credential migration idempotent");
    }
    finally
    {
        credential.DeleteApiKey();
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", originalConfigHome);
        Environment.SetEnvironmentVariable("ZVEC_DOCKER_CONFIG_HOME", originalHome);
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static async Task TestBackendInstanceRegistryContractsAsync()
{
    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-backend-registry-contract-{Guid.NewGuid():N}"
    );
    try
    {
        var configHome = Directory.CreateDirectory(Path.Combine(root, "config-home")).FullName;
        var queryRoot = Directory.CreateDirectory(Path.Combine(root, "queries")).FullName;
        var runtimeConfigPath = Path.GetFullPath(Path.Combine(root, "libraries.json"));
        await File.WriteAllTextAsync(runtimeConfigPath, "{}");
        var registry = new BackendInstanceRegistry(
            configHome,
            TimeSpan.FromMilliseconds(10)
        );
        var descriptor = CreateBackendInstanceDescriptor(queryRoot, runtimeConfigPath);

        await registry.WriteAsync(descriptor);
        var roundTrip = await registry.ReadAsync() ?? throw new Exception(
            "Backend instance descriptor was not persisted."
        );
        Assert(roundTrip.InstanceId == descriptor.InstanceId, "registry instance-id round trip");
        Assert(roundTrip.Host == "127.0.0.1", "registry loopback host round trip");
        Assert(roundTrip.Port == descriptor.Port, "registry port round trip");
        Assert(roundTrip.WrapperPid == descriptor.WrapperPid, "registry wrapper PID round trip");
        Assert(roundTrip.ServerPid == descriptor.ServerPid, "registry server PID round trip");
        Assert(
            roundTrip.ProcessStartUtc == descriptor.ProcessStartUtc,
            "registry process-start round trip"
        );
        Assert(roundTrip.IsReady, "registry ready state round trip");
        var persistedJson = await File.ReadAllTextAsync(registry.InstancePath);
        Assert(
            !persistedJson.Contains("token", StringComparison.OrdinalIgnoreCase),
            "registry never persists the backend token"
        );

        var finalBrace = persistedJson.LastIndexOf('}');
        Assert(finalBrace > 0, "registry contract fixture is a JSON object");
        var unknownFieldJson = persistedJson.Insert(
            finalBrace,
            ",\n  \"unexpected\": true\n"
        );
        await File.WriteAllTextAsync(registry.InstancePath, unknownFieldJson);
        await AssertThrowsAsync<InvalidDataException>(async () =>
            _ = await registry.ReadAsync()
        );

        var duplicateFieldJson = persistedJson.Replace(
            "\"instance_id\":",
            "\"instance_id\": \"duplicate\",\n  \"instance_id\":",
            StringComparison.Ordinal
        );
        await File.WriteAllTextAsync(registry.InstancePath, duplicateFieldJson);
        await AssertThrowsAsync<InvalidDataException>(async () =>
            _ = await registry.ReadAsync()
        );

        var relativePathDescriptor = CreateBackendInstanceDescriptor(
            "relative-query-root",
            runtimeConfigPath
        );
        await AssertThrowsAsync<InvalidDataException>(() =>
            registry.WriteAsync(relativePathDescriptor)
        );
        var remoteHostDescriptor = CreateBackendInstanceDescriptor(
            queryRoot,
            runtimeConfigPath,
            host: "0.0.0.0"
        );
        await AssertThrowsAsync<InvalidDataException>(() =>
            registry.WriteAsync(remoteHostDescriptor)
        );
        var invalidStateDescriptor = CreateBackendInstanceDescriptor(
            queryRoot,
            runtimeConfigPath,
            state: "stopped"
        );
        await AssertThrowsAsync<InvalidDataException>(() =>
            registry.WriteAsync(invalidStateDescriptor)
        );

        await registry.WriteAsync(descriptor);
        Assert(
            !await registry.DeleteIfMatchesAsync(descriptor.InstanceId + "-other"),
            "registry mismatch does not delete a newer instance"
        );
        Assert(File.Exists(registry.InstancePath), "registry mismatch preserves descriptor");
        Assert(
            !await registry.DeleteIfMatchesAsync(
                descriptor.InstanceId,
                descriptor.ProcessStartUtc.AddSeconds(-1)
            ),
            "registry process-start mismatch preserves descriptor"
        );
        Assert(
            await registry.DeleteIfMatchesAsync(
                descriptor.InstanceId,
                descriptor.ProcessStartUtc
            ),
            "registry matching identity deletes descriptor"
        );
        Assert(!File.Exists(registry.InstancePath), "registry matching delete removes file");
    }
    finally
    {
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static async Task TestBackendLaunchLockCancellationAsync()
{
    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-backend-lock-contract-{Guid.NewGuid():N}"
    );
    try
    {
        var registry = new BackendInstanceRegistry(
            Directory.CreateDirectory(Path.Combine(root, "config-home")).FullName,
            TimeSpan.FromMilliseconds(10)
        );
        var firstLock = await registry.AcquireLaunchLockAsync();
        try
        {
            Assert(firstLock.IsHeld, "first cross-process launch lock is held");
            using var cancellation = new CancellationTokenSource(
                TimeSpan.FromMilliseconds(150)
            );
            await AssertThrowsAsync<OperationCanceledException>(async () =>
                _ = await registry.AcquireLaunchLockAsync(cancellation.Token)
            );
        }
        finally
        {
            await firstLock.DisposeAsync();
        }
        Assert(!firstLock.IsHeld, "disposing launch lock releases its lease");

        using var reacquireTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await using var secondLock = await registry.AcquireLaunchLockAsync(
            reacquireTimeout.Token
        );
        Assert(secondLock.IsHeld, "launch lock can be reacquired after release");
    }
    finally
    {
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static async Task TestBackendConfigurationFingerprintContractsAsync()
{
    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-backend-fingerprint-contract-{Guid.NewGuid():N}"
    );
    try
    {
        var results = Directory.CreateDirectory(Path.Combine(root, "results")).FullName;
        var imageA = Directory.CreateDirectory(Path.Combine(root, "images-a")).FullName;
        var imageB = Directory.CreateDirectory(Path.Combine(root, "images-b")).FullName;
        var imageDisabled = Directory.CreateDirectory(
            Path.Combine(root, "images-disabled")
        ).FullName;
        var workspaceA = Directory.CreateDirectory(Path.Combine(root, "workspace-a")).FullName;
        var workspaceB = Directory.CreateDirectory(Path.Combine(root, "workspace-b")).FullName;
        var workspaceDisabled = Directory.CreateDirectory(
            Path.Combine(root, "workspace-disabled")
        ).FullName;
        var entryPoint = await BackendPayloadContractFixture.CreateAsync(root);

        var baselineConfig = new LauncherConfig
        {
            SchemaVersion = LauncherConfig.CurrentSchemaVersion,
            ResultsDirectory = results,
            DefaultLibraryId = "library-a",
            Libraries =
            [
                new LauncherLibrary
                {
                    Id = "library-a",
                    Name = "Library A",
                    ImageRoot = imageA,
                    WorkspaceDirectory = workspaceA,
                    Enabled = true,
                },
                new LauncherLibrary
                {
                    Id = "library-b",
                    Name = "Library B",
                    ImageRoot = imageB,
                    WorkspaceDirectory = workspaceB,
                    Enabled = true,
                },
                new LauncherLibrary
                {
                    Id = "disabled",
                    Name = "Ignored library",
                    ImageRoot = imageDisabled,
                    WorkspaceDirectory = workspaceDisabled,
                    Enabled = false,
                },
            ],
        };
        var baseline = await BackendConfigurationFingerprint.ComputeAsync(
            baselineConfig,
            entryPoint
        );

        var reorderedConfig = new LauncherConfig
        {
            SchemaVersion = LauncherConfig.CurrentSchemaVersion,
            ResultsDirectory = results + Path.DirectorySeparatorChar,
            DefaultLibraryId = "LIBRARY-A",
            Libraries =
            [
                new LauncherLibrary
                {
                    Id = "LIBRARY-B",
                    Name = " Library B ",
                    ImageRoot = imageB + Path.DirectorySeparatorChar,
                    WorkspaceDirectory = workspaceB + Path.DirectorySeparatorChar,
                    Enabled = true,
                },
                new LauncherLibrary
                {
                    Id = "LIBRARY-A",
                    Name = "Library A",
                    ImageRoot = imageA + Path.DirectorySeparatorChar,
                    WorkspaceDirectory = workspaceA + Path.DirectorySeparatorChar,
                    Enabled = true,
                },
                new LauncherLibrary
                {
                    Id = "changed-disabled",
                    Name = "Disabled changes are ignored",
                    ImageRoot = root,
                    WorkspaceDirectory = root,
                    Enabled = false,
                },
            ],
        };
        var reordered = await BackendConfigurationFingerprint.ComputeAsync(
            reorderedConfig,
            entryPoint
        );
        Assert(
            baseline == reordered,
            "fingerprint is stable across effective ordering and normalized spelling"
        );

        reorderedConfig.Libraries[0].Name = "Library B renamed";
        var effectiveConfigChanged = await BackendConfigurationFingerprint.ComputeAsync(
            reorderedConfig,
            entryPoint
        );
        Assert(
            baseline != effectiveConfigChanged,
            "fingerprint changes when effective library configuration changes"
        );

        reorderedConfig.Libraries[0].Name = "Library B";
        var originalEntryPoint = await File.ReadAllTextAsync(entryPoint);
        await File.WriteAllTextAsync(entryPoint, originalEntryPoint + "# changed\n");
        var entryPointChanged = await BackendConfigurationFingerprint.ComputeAsync(
            reorderedConfig,
            entryPoint
        );
        Assert(
            baseline != entryPointChanged,
            "fingerprint changes when backend entry-point content changes"
        );

        await File.WriteAllTextAsync(entryPoint, originalEntryPoint);
        Assert(
            baseline == await BackendConfigurationFingerprint.ComputeAsync(
                reorderedConfig,
                entryPoint
            ),
            "restoring backend entry-point content restores the fingerprint"
        );

        foreach (var relativePath in new[]
        {
            "zvec_logging.py",
            "requirements-lock.txt",
            "model-catalog.default.json",
            "pyproject.toml",
            Path.Combine("image_vector_service", "backend_server.py"),
        })
        {
            var runtimeFile = Path.Combine(root, relativePath);
            var originalContent = await File.ReadAllTextAsync(runtimeFile);
            await File.WriteAllTextAsync(runtimeFile, originalContent + "# changed\n");
            Assert(
                baseline != await BackendConfigurationFingerprint.ComputeAsync(
                    reorderedConfig,
                    entryPoint
                ),
                $"fingerprint covers runtime payload file {relativePath}"
            );
            await File.WriteAllTextAsync(runtimeFile, originalContent);
        }

        var addedModule = Path.Combine(
            root,
            "image_vector_service",
            "new_runtime_module.py"
        );
        await File.WriteAllTextAsync(addedModule, "# newly published module\n");
        Assert(
            baseline != await BackendConfigurationFingerprint.ComputeAsync(
                reorderedConfig,
                entryPoint
            ),
            "fingerprint covers newly published package modules"
        );
        File.Delete(addedModule);

        var modelCatalog = Path.Combine(root, "model-catalog.default.json");
        var originalModelCatalog = await File.ReadAllTextAsync(modelCatalog);
        await File.WriteAllTextAsync(modelCatalog, "{\"schema_version\":1}\n");
        var catalogFingerprint = await BackendConfigurationFingerprint.ComputeAsync(
            reorderedConfig,
            entryPoint
        );
        Assert(
            baseline != catalogFingerprint,
            "fingerprint covers the required published model catalog"
        );
        await File.WriteAllTextAsync(modelCatalog, "{\"schema_version\":2}\n");
        Assert(
            catalogFingerprint != await BackendConfigurationFingerprint.ComputeAsync(
                reorderedConfig,
                entryPoint
            ),
            "fingerprint covers published model catalog content"
        );
        await File.WriteAllTextAsync(modelCatalog, originalModelCatalog);

        var mirrorRoot = Path.Combine(root, "payload-mirror");
        var mirrorEntryPoint = await BackendPayloadContractFixture.CreateAsync(
            mirrorRoot,
            reverseCreationOrder: true
        );
        Assert(
            await BackendConfigurationFingerprint.ComputeRuntimePayloadHashAsync(entryPoint) ==
                await BackendConfigurationFingerprint.ComputeRuntimePayloadHashAsync(
                    mirrorEntryPoint
                ),
            "runtime payload hash is independent of root and creation order"
        );

        var lockFile = Path.Combine(root, "requirements-lock.txt");
        var savedLockFile = lockFile + ".saved";
        File.Move(lockFile, savedLockFile);
        try
        {
            await AssertThrowsAsync<FileNotFoundException>(() =>
                BackendConfigurationFingerprint.ComputeAsync(
                    reorderedConfig,
                    entryPoint
                )
            );
        }
        finally
        {
            File.Move(savedLockFile, lockFile);
        }

        var savedModelCatalog = modelCatalog + ".saved";
        File.Move(modelCatalog, savedModelCatalog);
        try
        {
            await AssertThrowsAsync<FileNotFoundException>(() =>
                BackendConfigurationFingerprint.ComputeAsync(
                    reorderedConfig,
                    entryPoint
                )
            );
        }
        finally
        {
            File.Move(savedModelCatalog, modelCatalog);
        }

        var backendServer = Path.Combine(
            root,
            "image_vector_service",
            "backend_server.py"
        );
        var savedBackendServer = backendServer + ".saved";
        var outsideModule = Path.Combine(
            Path.GetTempPath(),
            $"zvec-backend-fingerprint-outside-{Guid.NewGuid():N}.py"
        );
        await File.WriteAllTextAsync(outsideModule, "# outside payload\n");
        File.Move(backendServer, savedBackendServer);
        try
        {
            if (TryCreateFileSymbolicLink(backendServer, outsideModule))
            {
                await AssertThrowsAsync<InvalidDataException>(() =>
                    BackendConfigurationFingerprint.ComputeAsync(
                        reorderedConfig,
                        entryPoint
                    )
                );
            }
        }
        finally
        {
            File.Delete(backendServer);
            File.Move(savedBackendServer, backendServer);
            File.Delete(outsideModule);
        }
    }
    finally
    {
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static void TestBackendSessionTokenStoreContracts()
{
    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-backend-token-contract-{Guid.NewGuid():N}"
    );
    try
    {
        var configHomeA = Directory.CreateDirectory(Path.Combine(root, "config-a")).FullName;
        var configHomeB = Directory.CreateDirectory(Path.Combine(root, "config-b")).FullName;
        var fakeSecret = new FakeSecureSecretStore();
        var tokenStore = new BackendSessionTokenStore(configHomeA, fakeSecret);
        var equivalentTarget = BackendSessionTokenStore.CreateCredentialTarget(
            Path.Combine(configHomeA, ".") + Path.DirectorySeparatorChar
        );
        var otherTarget = BackendSessionTokenStore.CreateCredentialTarget(configHomeB);

        Assert(
            tokenStore.CredentialTarget == equivalentTarget,
            "credential target uses normalized config-home identity"
        );
        Assert(
            tokenStore.CredentialTarget != otherTarget,
            "different config homes use isolated backend credentials"
        );
        Assert(
            !string.Equals(
                tokenStore.CredentialTarget,
                "Zvec.ImageSearch/DashScopeApiKey",
                StringComparison.Ordinal
            ),
            "backend token target never overwrites the DashScope API key"
        );
        Assert(
            !tokenStore.CredentialTarget.Contains(configHomeA, StringComparison.OrdinalIgnoreCase),
            "credential target contains only the config-home hash"
        );

        tokenStore.SaveToken("contract-token-new");
        Assert(tokenStore.HasToken(), "fake secure store receives backend token");
        Assert(tokenStore.ReadToken() == "contract-token-new", "backend token round trip");
        Assert(
            !tokenStore.DeleteIfMatches("contract-token-old"),
            "stale backend cannot delete a replacement token"
        );
        Assert(
            fakeSecret.Value == "contract-token-new" && fakeSecret.DeleteCalls == 0,
            "token mismatch preserves replacement secret"
        );
        Assert(
            tokenStore.DeleteIfMatches("contract-token-new"),
            "matching backend token can be deleted"
        );
        Assert(!tokenStore.HasToken(), "matching token delete clears secure store");
        Assert(fakeSecret.DeleteCalls == 1, "matching token is deleted exactly once");
    }
    finally
    {
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static async Task TestLiveBackendWithoutTokenIsPreservedAsync()
{
    var originalConfigHome = Environment.GetEnvironmentVariable("ZVEC_CONFIG_HOME");
    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-backend-missing-token-contract-{Guid.NewGuid():N}"
    );
    var configHome = Directory.CreateDirectory(Path.Combine(root, "config-home")).FullName;
    var tokenStore = new BackendSessionTokenStore(configHome);
    try
    {
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", configHome);
        tokenStore.DeleteToken();
        var images = Directory.CreateDirectory(Path.Combine(root, "images")).FullName;
        var workspace = Directory.CreateDirectory(Path.Combine(root, "workspace")).FullName;
        var results = Directory.CreateDirectory(Path.Combine(root, "results")).FullName;
        var queryRoot = Directory.CreateDirectory(Path.Combine(root, "queries")).FullName;
        var runtimeConfigPath = Path.GetFullPath(Path.Combine(root, "libraries.json"));
        await File.WriteAllTextAsync(runtimeConfigPath, "{\"schema_version\":3}\n");
        var entryPoint = await BackendPayloadContractFixture.CreateAsync(
            root,
            "missing-token"
        );
        var config = new LauncherConfig
        {
            SchemaVersion = LauncherConfig.CurrentSchemaVersion,
            PythonExecutable = Path.Combine(root, "must-not-start-python.exe"),
            ResultsDirectory = results,
            DefaultLibraryId = "library-a",
            Libraries =
            [
                new LauncherLibrary
                {
                    Id = "library-a",
                    Name = "Missing token contract",
                    ImageRoot = images,
                    WorkspaceDirectory = workspace,
                    Enabled = true,
                },
            ],
        };
        var fingerprint = await BackendConfigurationFingerprint.ComputeAsync(
            config,
            entryPoint
        );
        using var currentProcess = Process.GetCurrentProcess();
        var processStartUtc = new DateTimeOffset(
            currentProcess.StartTime.ToUniversalTime(),
            TimeSpan.Zero
        );
        var descriptor = new BackendInstanceDescriptor
        {
            InstanceId = $"live-missing-token-{Guid.NewGuid():N}",
            Host = "127.0.0.1",
            Port = 18766,
            WrapperPid = currentProcess.Id,
            ProcessStartUtc = processStartUtc,
            ConfigFingerprint = fingerprint,
            QueryRoot = Path.GetFullPath(queryRoot),
            RuntimeConfigPath = runtimeConfigPath,
            State = BackendInstanceStates.Ready,
            GeneratedUtc = DateTimeOffset.UtcNow,
        };
        var registry = new BackendInstanceRegistry(configHome);
        await registry.WriteAsync(descriptor);
        var options = new BackendHostOptions
        {
            AutoPrepareRuntime = false,
            PythonExecutable = config.PythonExecutable,
            BackendEntryPointPath = entryPoint,
            ApplicationDirectory = root,
            StartupTimeout = TimeSpan.FromMilliseconds(250),
            HealthPollInterval = TimeSpan.FromMilliseconds(25),
            HealthRequestTimeout = TimeSpan.FromMilliseconds(50),
            ShutdownTimeout = TimeSpan.FromMilliseconds(250),
        };
        var processFactory = new FakeBackendProcessFactory();
        await using var host = new BackendHostService(
            config,
            options,
            processFactory
        );

        var exception = await CaptureExceptionAsync<BackendProtocolException>(
            async () => await host.StartAsync()
        );
        Assert(
            exception.Message.Contains("会话凭据已丢失", StringComparison.Ordinal),
            "missing-token failure explains why the live instance is preserved"
        );
        Assert(
            (await registry.ReadAsync())?.InstanceId == descriptor.InstanceId,
            "missing token never deletes a live backend descriptor"
        );
        Assert(
            processFactory.Process.StartInfo is null,
            "missing token never starts a competing backend process"
        );

        var unknownProcessStartUtc = DateTimeOffset.UtcNow.AddSeconds(-1);
        var descriptorWithoutPid = new BackendInstanceDescriptor
        {
            InstanceId = $"unknown-missing-token-{Guid.NewGuid():N}",
            Host = "127.0.0.1",
            Port = 18767,
            ProcessStartUtc = unknownProcessStartUtc,
            ConfigFingerprint = fingerprint,
            QueryRoot = Path.GetFullPath(queryRoot),
            RuntimeConfigPath = runtimeConfigPath,
            State = BackendInstanceStates.Ready,
            GeneratedUtc = DateTimeOffset.UtcNow,
        };
        await registry.WriteAsync(descriptorWithoutPid);
        var unknownProcessFactory = new FakeBackendProcessFactory();
        await using var unknownHost = new BackendHostService(
            config,
            options,
            unknownProcessFactory
        );
        _ = await CaptureExceptionAsync<BackendProtocolException>(
            async () => await unknownHost.StartAsync()
        );
        Assert(
            (await registry.ReadAsync())?.InstanceId == descriptorWithoutPid.InstanceId,
            "missing token preserves a descriptor whose process identity is unknown"
        );
        Assert(
            unknownProcessFactory.Process.StartInfo is null,
            "unknown process identity never starts a competing backend"
        );
    }
    finally
    {
        tokenStore.DeleteToken();
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", originalConfigHome);
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static BackendInstanceDescriptor CreateBackendInstanceDescriptor(
    string queryRoot,
    string runtimeConfigPath,
    string host = "127.0.0.1",
    string state = BackendInstanceStates.Ready)
{
    var processStartUtc = DateTimeOffset.UtcNow.AddSeconds(-1);
    return new BackendInstanceDescriptor
    {
        InstanceId = $"zvec-backend-contract-{Guid.NewGuid():N}",
        Host = host,
        Port = 18765,
        WrapperPid = Environment.ProcessId,
        ServerPid = Environment.ProcessId + 1,
        ProcessStartUtc = processStartUtc,
        ConfigFingerprint = new string('a', 64),
        QueryRoot = queryRoot,
        RuntimeConfigPath = runtimeConfigPath,
        State = state,
        GeneratedUtc = processStartUtc.AddMilliseconds(500),
    };
}

static void TestBackendCapabilityCompatibility()
{
    var legacy = JsonSerializer.Deserialize<BackendVersionResponse>(
        """
        {"protocol_version":2,"app":"zvec-image-search","version":"0.4.0","capabilities":{"multi_library":true,"federated_search":true,"session_credentials":true}}
        """
    ) ?? throw new Exception("Legacy backend version response was not parsed.");
    Assert(!legacy.Capabilities.LowConfidenceOverride, "legacy capability defaults off");
    Assert(!legacy.Capabilities.TagOnlySearch, "legacy tag-only capability defaults off");
    Assert(!legacy.Capabilities.HybridTagVectorSearch, "legacy hybrid capability defaults off");
    Assert(
        !legacy.Capabilities.MetadataEmbeddingSearch,
        "legacy metadata search capability defaults off"
    );
    Assert(
        !legacy.Capabilities.MetadataEmbeddingBackfill,
        "legacy metadata backfill capability defaults off"
    );
    Assert(!legacy.Capabilities.ResultDiversity, "legacy diversity capability defaults off");
    Assert(!legacy.Capabilities.IndexAndAutoTag, "legacy combined capability defaults off");

    var current = JsonSerializer.Deserialize<BackendVersionResponse>(
        """
        {"protocol_version":2,"app":"zvec-image-search","version":"0.4.0","capabilities":{"multi_library":true,"federated_search":true,"session_credentials":true,"low_confidence_override":true,"tag_only_search":true,"hybrid_tag_vector_search":true,"metadata_embedding_search":true,"metadata_embedding_backfill":true,"result_diversity":true,"index_and_auto_tag":true}}
        """
    ) ?? throw new Exception("Current backend version response was not parsed.");
    Assert(current.Capabilities.LowConfidenceOverride, "current capability is detected");
    Assert(current.Capabilities.TagOnlySearch, "tag-only capability is detected");
    Assert(current.Capabilities.HybridTagVectorSearch, "hybrid capability is detected");
    Assert(
        current.Capabilities.MetadataEmbeddingSearch,
        "metadata search capability is detected"
    );
    Assert(
        current.Capabilities.MetadataEmbeddingBackfill,
        "metadata backfill capability is detected"
    );
    Assert(current.Capabilities.ResultDiversity, "diversity capability is detected");
    Assert(current.Capabilities.IndexAndAutoTag, "combined capability is detected");
}

static void TestBackendSearchRequestCompatibility()
{
    var defaultParameters = new Dictionary<string, object?> { ["text"] = "sunset" };
    BackendSearchRequestOptions.AddSearchMode(
        defaultParameters,
        semanticEnabled: true,
        tagOnlySearchSupported: false
    );
    Assert(
        !defaultParameters.ContainsKey("search_mode"),
        "semantic request omits optional search mode for old backends"
    );
    BackendSearchRequestOptions.AddLowConfidenceOverride(
        defaultParameters,
        requested: false,
        supported: false
    );
    Assert(
        !defaultParameters.ContainsKey("show_low_confidence"),
        "default request omits optional field for old backends"
    );
    BackendSearchRequestOptions.AddResultDiversity(
        defaultParameters,
        requested: true,
        supported: false
    );
    Assert(
        !defaultParameters.ContainsKey("diversify_results"),
        "default diversity request omits optional field for old backends"
    );

    var supportedParameters = new Dictionary<string, object?> { ["text"] = "sunset" };
    BackendSearchRequestOptions.AddLowConfidenceOverride(
        supportedParameters,
        requested: true,
        supported: true
    );
    Assert(
        supportedParameters.TryGetValue("show_low_confidence", out var value) &&
            value is true,
        "supported request sends explicit override"
    );
    BackendSearchRequestOptions.AddResultDiversity(
        supportedParameters,
        requested: false,
        supported: true
    );
    Assert(
        supportedParameters.TryGetValue("diversify_results", out var diversityValue) &&
            diversityValue is false,
        "supported request can restore the original series order"
    );

    var tagOnlyParameters = new Dictionary<string, object?> { ["text"] = "神" };
    BackendSearchRequestOptions.AddSearchMode(
        tagOnlyParameters,
        semanticEnabled: false,
        tagOnlySearchSupported: true
    );
    Assert(
        tagOnlyParameters.TryGetValue("search_mode", out var searchMode) &&
            string.Equals(searchMode as string, "tags", StringComparison.Ordinal),
        "supported tag-only request sends the explicit mode"
    );

    try
    {
        BackendSearchRequestOptions.AddSearchMode(
            new Dictionary<string, object?> { ["text"] = "神" },
            semanticEnabled: false,
            tagOnlySearchSupported: false
        );
        throw new Exception("Unsupported tag-only request did not fail.");
    }
    catch (InvalidOperationException exception)
    {
        Assert(
            exception.Message.Contains("纯标签搜索", StringComparison.Ordinal),
            "old backend tag-search guidance"
        );
    }

    var unsupportedParameters = new Dictionary<string, object?> { ["text"] = "sunset" };
    try
    {
        BackendSearchRequestOptions.AddLowConfidenceOverride(
            unsupportedParameters,
            requested: true,
            supported: false
        );
        throw new Exception("Unsupported override request did not fail.");
    }
    catch (InvalidOperationException exception)
    {
        Assert(exception.Message.Contains("更新桌面应用", StringComparison.Ordinal), "old backend guidance");
    }

    try
    {
        BackendSearchRequestOptions.AddResultDiversity(
            new Dictionary<string, object?> { ["text"] = "sunset" },
            requested: false,
            supported: false
        );
        throw new Exception("Unsupported diversity override did not fail.");
    }
    catch (InvalidOperationException exception)
    {
        Assert(
            exception.Message.Contains("套图多样化", StringComparison.Ordinal),
            "old backend diversity guidance"
        );
    }
}

static async Task TestBackendProtocolValidationAsync()
{
    const string jobId = "0123456789abcdef0123456789abcdef";
    using var httpClient = new HttpClient(new StaticJsonHandler(
        """
        {"job":{"id":"0123456789abcdef0123456789abcdef","command":"stats","status":"unknown"}}
        """
    ));
    using var client = new BackendApiClient(
        new Uri("http://127.0.0.1:8765/"),
        "contract-token",
        httpClient
    );
    await AssertThrowsAsync<BackendProtocolException>(() => client.GetJobAsync(jobId));
}

static async Task TestBackendLifecycleCancellationAsync()
{
    using var lifecycle = new BackendLifecycleCoordinator();
    var entered = new TaskCompletionSource(
        TaskCreationOptions.RunContinuationsAsynchronously
    );
    var cleanupCompleted = false;
    var startup = lifecycle.RunStartupAsync(async cancellationToken =>
    {
        entered.TrySetResult();
        try
        {
            await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
            return true;
        }
        finally
        {
            cleanupCompleted = true;
        }
    });

    await entered.Task.WaitAsync(TimeSpan.FromSeconds(2));
    var stopwatch = Stopwatch.StartNew();
    lifecycle.RequestShutdown();
    await lifecycle.RunExclusiveAsync(() => Task.CompletedTask).WaitAsync(
        TimeSpan.FromSeconds(2)
    );
    await AssertThrowsAsync<OperationCanceledException>(async () => await startup);

    Assert(cleanupCompleted, "shutdown waits for startup cancellation cleanup");
    Assert(stopwatch.Elapsed < TimeSpan.FromSeconds(2), "shutdown gate is cancellation bounded");
}

static async Task TestRuntimeBootstrapContractsAsync()
{
    var runtimePython = Path.Combine(
        Path.GetTempPath(),
        "zvec-runtime-contract",
        "venv",
        "Scripts",
        "python.exe"
    );
    var successPayload = JsonSerializer.Serialize(new
    {
        schema_version = 1,
        success = true,
        status = "repaired",
        changed = true,
        code = "repaired",
        message = "runtime ready",
        recommended_action = (string?)null,
        python_executable = runtimePython,
        python_version = "3.14.0",
        python_architecture = "AMD64",
        runtime_directory = Path.GetDirectoryName(Path.GetDirectoryName(runtimePython)),
        dependencies = new[]
        {
            new { name = "zvec", version = "0.5.1", imported = true },
            new { name = "Pillow", version = "11.3.0", imported = true },
            new { name = "numpy", version = "2.3.2", imported = true },
        },
    });
    var runner = new FakeRuntimeBootstrapCommandRunner(command => new CommandResult(
        0,
        $"preparing wheel{Environment.NewLine}" +
            $"{RuntimeBootstrapService.ResultPrefix}{successPayload}{Environment.NewLine}",
        string.Empty,
        WasCancelled: false
    ));
    using var service = new RuntimeBootstrapService(runner);
    var result = await service.EnsureReadyAsync("C:\\Python314\\python.exe");

    Assert(result.IsSuccess, "runtime bootstrap success parsed");
    Assert(result.Changed && result.Status == "repaired", "runtime repair state parsed");
    Assert(result.PythonExecutable == runtimePython, "isolated Python path parsed");
    Assert(result.PythonVersion == "3.14.0", "Python version parsed");
    Assert(result.PythonArchitecture == "AMD64", "Python architecture parsed");
    Assert(result.Dependencies.Count == 3, "runtime dependency count parsed");
    Assert(result.Dependencies.All(dependency => dependency.Imported), "imports verified");
    Assert(runner.LastCommand == "runtime-bootstrap", "repair command selected");
    Assert(
        runner.LastEnvironment?["ZVEC_PYTHON"] == "C:\\Python314\\python.exe",
        "selected bootstrap interpreter forwarded without command-line interpolation"
    );
    Assert(
        result.DiagnosticOutput.Contains("preparing wheel", StringComparison.Ordinal),
        "bootstrap diagnostics retained"
    );

    var diagnosis = await service.DiagnoseAsync();
    Assert(diagnosis.IsSuccess, "runtime doctor success parsed");
    Assert(runner.LastCommand == "runtime-doctor", "doctor command selected");

    var architectureFailurePayload = JsonSerializer.Serialize(new
    {
        schema_version = 1,
        success = false,
        status = "failed",
        changed = false,
        code = "windows_arm64_python_unsupported",
        message = "ARM64 Python is unsupported",
        recommended_action = "Install x64 CPython and retry.",
        python_executable = (string?)null,
        python_version = (string?)null,
        python_architecture = "ARM64",
        runtime_directory = (string?)null,
        dependencies = Array.Empty<object>(),
    });
    var failureRunner = new FakeRuntimeBootstrapCommandRunner(_ => new CommandResult(
        1,
        $"{RuntimeBootstrapService.ResultPrefix}{architectureFailurePayload}\n",
        "bootstrap failed",
        WasCancelled: false
    ));
    using var failureService = new RuntimeBootstrapService(failureRunner);
    var failure = await failureService.EnsureReadyAsync();
    Assert(!failure.IsSuccess, "runtime failure parsed");
    Assert(
        failure.Code == "windows_arm64_python_unsupported",
        "Windows ARM64 interpreter limitation is structured"
    );
    Assert(
        failure.RecommendedAction?.Contains("x64 CPython", StringComparison.Ordinal) == true,
        "runtime failure includes actionable recovery"
    );
    var exception = await CaptureExceptionAsync<RuntimeBootstrapException>(
        () => failureService.EnsureReadyOrThrowAsync()
    );
    Assert(exception.Result.Code == failure.Code, "throwing bootstrap API preserves result");

    var incompletePayload = JsonSerializer.Serialize(new
    {
        schema_version = 1,
        success = true,
        status = "ready",
        changed = false,
        code = "ready",
        message = "runtime ready",
        python_executable = runtimePython,
        python_version = "3.14.0",
        python_architecture = "AMD64",
        dependencies = new[]
        {
            new { name = "zvec", version = "0.5.1", imported = true },
            new { name = "Pillow", version = "11.3.0", imported = true },
        },
    });
    var incomplete = RuntimeBootstrapService.ParseCommandResult(new CommandResult(
        0,
        $"{RuntimeBootstrapService.ResultPrefix}{incompletePayload}\n",
        string.Empty,
        WasCancelled: false
    ));
    Assert(
        incomplete.Code == "bootstrap_verification_incomplete",
        "success requires numpy as well as zvec and Pillow imports"
    );

    var cancelledRunner = new FakeRuntimeBootstrapCommandRunner(_ => new CommandResult(
        -1,
        string.Empty,
        string.Empty,
        WasCancelled: true
    ));
    using var cancelledService = new RuntimeBootstrapService(cancelledRunner);
    var cancelled = await cancelledService.EnsureReadyAsync();
    Assert(cancelled.Status == "cancelled", "runtime cancellation is structured");

    var sharedRunner = new FakeRuntimeBootstrapCommandRunner(_ => new CommandResult(
        0,
        $"{RuntimeBootstrapService.ResultPrefix}{successPayload}\n",
        string.Empty,
        WasCancelled: false
    ));
    var sharedService = new RuntimeBootstrapService(sharedRunner);
    sharedService.Dispose();
    Assert(!sharedRunner.IsDisposed, "bootstrap service does not dispose shared UI runner");
}

static async Task TestBackendStartupCancellationStopsNativeProcessAsync()
{
    var originalConfigHome = Environment.GetEnvironmentVariable("ZVEC_CONFIG_HOME");
    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-backend-cancellation-{Guid.NewGuid():N}"
    );
    try
    {
        var images = Directory.CreateDirectory(Path.Combine(root, "images")).FullName;
        var workspace = Directory.CreateDirectory(Path.Combine(root, "workspace")).FullName;
        var results = Directory.CreateDirectory(Path.Combine(root, "results")).FullName;
        var entryPoint = await BackendPayloadContractFixture.CreateAsync(
            root,
            "startup-cancellation"
        );
        var configHome = Directory.CreateDirectory(Path.Combine(root, "config-home")).FullName;
        var pythonExecutable = Path.Combine(
            configHome,
            "runtime",
            "venv",
            "Scripts",
            "python.exe"
        );
        Directory.CreateDirectory(Path.GetDirectoryName(pythonExecutable)!);
        await File.WriteAllBytesAsync(pythonExecutable, [0x4d, 0x5a]);
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", configHome);
        var config = new LauncherConfig
        {
            SchemaVersion = LauncherConfig.CurrentSchemaVersion,
            ResultsDirectory = results,
            DefaultLibraryId = "library-a",
            Libraries =
            [
                new LauncherLibrary
                {
                    Id = "library-a",
                    Name = "Contract library",
                    ImageRoot = images,
                    WorkspaceDirectory = workspace,
                    Enabled = true,
                },
            ],
        };
        var processFactory = new FakeBackendProcessFactory();
        var runtimeBootstrapper = new FakeRuntimeBootstrapper(pythonExecutable);
        await using var backend = new BackendHostService(
            config,
            new BackendHostOptions
            {
                BackendEntryPointPath = entryPoint,
                ApplicationDirectory = root,
                StartupTimeout = TimeSpan.FromSeconds(30),
                HealthPollInterval = TimeSpan.FromMilliseconds(25),
                HealthRequestTimeout = TimeSpan.FromMilliseconds(50),
                ShutdownTimeout = TimeSpan.FromMilliseconds(250),
            },
            processFactory,
            runtimeBootstrapper
        );
        using var cancellation = new CancellationTokenSource();
        var startup = backend.StartAsync(cancellation.Token);

        await processFactory.Process.Started.Task.WaitAsync(TimeSpan.FromSeconds(2));
        var stopwatch = Stopwatch.StartNew();
        cancellation.Cancel();
        await AssertThrowsAsync<OperationCanceledException>(async () => await startup);

        Assert(stopwatch.Elapsed < TimeSpan.FromSeconds(2), "backend startup cancellation is bounded");
        Assert(processFactory.Process.StopCalls == 1, "cancelled startup stops native process once");
        Assert(processFactory.Process.HasExited, "cancelled startup leaves no native backend");
        var startInfo = processFactory.Process.StartInfo ?? throw new Exception(
            "Native backend start info was not captured."
        );
        Assert(startInfo.FileName == pythonExecutable, "config-home isolated Python used");
        Assert(runtimeBootstrapper.Calls == 1, "native runtime prepared before backend start");
        Assert(!startInfo.FileName.Contains("docker", StringComparison.OrdinalIgnoreCase), "Docker is not invoked");
        var launchArguments = startInfo.ArgumentList.ToArray();
        Assert(launchArguments[0] == entryPoint, "image_service entry point used");
        Assert(launchArguments.Contains("serve", StringComparer.Ordinal), "serve command used");
        Assert(!launchArguments.Contains("--token", StringComparer.Ordinal), "session token omitted from arguments");
        Assert(
            startInfo.Environment.TryGetValue("ZVEC_BACKEND_TOKEN", out var backendToken) &&
                !string.IsNullOrWhiteSpace(backendToken),
            "session token passed only through the child environment"
        );
        Assert(launchArguments.Contains("--libraries-config", StringComparer.Ordinal), "runtime config passed");
        Assert(launchArguments.Contains("--instance-id", StringComparer.Ordinal), "instance identity passed");
        Assert(launchArguments.Contains("--config-fingerprint", StringComparer.Ordinal), "config fingerprint passed");
        Assert(launchArguments.Contains("--instance-lock-path", StringComparer.Ordinal), "instance lock passed");
        Assert(!startInfo.Environment.ContainsKey("DASHSCOPE_API_KEY"), "API key not inherited by process");
        using var runtimeConfig = JsonDocument.Parse(processFactory.Process.RuntimeConfigJson);
        var runtimeRoot = runtimeConfig.RootElement;
        Assert(runtimeRoot.GetProperty("schema_version").GetInt32() == 3, "native runtime schema");
        var runtimeLibrary = runtimeRoot.GetProperty("libraries")[0];
        Assert(runtimeLibrary.GetProperty("workspace_directory").GetString() == workspace, "native workspace path");
        Assert(!runtimeLibrary.TryGetProperty("workspace_source", out _), "runtime omits Docker source");
        Assert(!runtimeLibrary.TryGetProperty("workspace_type", out _), "runtime omits Docker type");
    }
    finally
    {
        Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", originalConfigHome);
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static async Task TestBackendCommandCancellationTerminatesProcessAsync()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-command-cancellation-{Guid.NewGuid():N}"
    );
    Directory.CreateDirectory(root);
    var pidPath = Path.Combine(root, "pid.txt");
    try
    {
        var escapedPidPath = pidPath.Replace("'", "''", StringComparison.Ordinal);
        var startInfo = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        startInfo.ArgumentList.Add("-NoLogo");
        startInfo.ArgumentList.Add("-NoProfile");
        startInfo.ArgumentList.Add("-NonInteractive");
        startInfo.ArgumentList.Add("-Command");
        startInfo.ArgumentList.Add(
            $"[IO.File]::WriteAllText('{escapedPidPath}', $PID.ToString()); " +
                "Start-Sleep -Seconds 30"
        );

        var runner = new BackendCommandRunner();
        using var cancellation = new CancellationTokenSource();
        var command = runner.RunAsync(
            startInfo,
            TimeSpan.FromSeconds(30),
            cancellation.Token
        );
        await WaitForFileAsync(pidPath, TimeSpan.FromSeconds(3));
        var childProcessId = int.Parse(
            await File.ReadAllTextAsync(pidPath),
            System.Globalization.CultureInfo.InvariantCulture
        );

        var stopwatch = Stopwatch.StartNew();
        cancellation.Cancel();
        await AssertThrowsAsync<OperationCanceledException>(async () => await command);
        Assert(stopwatch.Elapsed < TimeSpan.FromSeconds(3), "cancelled command exits promptly");
        AssertProcessExited(childProcessId, "cancelled command process tree exited");
    }
    finally
    {
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static async Task TestPowerShellStartupCancellationTerminatesProcessAsync()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-powershell-cancellation-{Guid.NewGuid():N}"
    );
    var scripts = Directory.CreateDirectory(Path.Combine(root, "scripts")).FullName;
    var pidPath = Path.Combine(root, "pid.txt");
    try
    {
        await File.WriteAllTextAsync(
            Path.Combine(scripts, "zvec.ps1"),
            "param([string]$Command)\n" +
                "[IO.File]::WriteAllText($env:ZVEC_TEST_PID_PATH, $PID.ToString())\n" +
                "Start-Sleep -Seconds 30\n",
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false)
        );

        using var runner = new PowerShellZvecRunner(root);
        using var cancellation = new CancellationTokenSource();
        var command = runner.RunZvecAsync(
            "doctor",
            environment: new Dictionary<string, string?>
            {
                ["ZVEC_TEST_PID_PATH"] = pidPath,
            },
            cancellationToken: cancellation.Token
        );
        await WaitForFileAsync(pidPath, TimeSpan.FromSeconds(3));
        var childProcessId = int.Parse(
            await File.ReadAllTextAsync(pidPath),
            System.Globalization.CultureInfo.InvariantCulture
        );

        var stopwatch = Stopwatch.StartNew();
        cancellation.Cancel();
        var result = await command.WaitAsync(TimeSpan.FromSeconds(3));
        Assert(result.WasCancelled, "PowerShell startup reports cancellation");
        Assert(stopwatch.Elapsed < TimeSpan.FromSeconds(3), "PowerShell startup cancellation is bounded");
        AssertProcessExited(childProcessId, "PowerShell startup process tree exited");
    }
    finally
    {
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static async Task WaitForFileAsync(string path, TimeSpan timeout)
{
    var stopwatch = Stopwatch.StartNew();
    while (!File.Exists(path) && stopwatch.Elapsed < timeout)
    {
        await Task.Delay(25);
    }
    Assert(File.Exists(path), $"timed out waiting for {Path.GetFileName(path)}");
}

static void AssertProcessExited(int processId, string contract)
{
    try
    {
        using var process = Process.GetProcessById(processId);
        Assert(process.HasExited, contract);
    }
    catch (ArgumentException)
    {
        // The process no longer exists, which is the expected state.
    }
}

static async Task TestJsonRequestsHaveContentLengthAsync()
{
    using var handler = new CapturingJsonHandler();
    using var httpClient = new HttpClient(handler);
    using var client = new BackendApiClient(
        new Uri("http://127.0.0.1:8765/"),
        "contract-token",
        httpClient
    );

    await client.ConfigureCredentialsAsync(
        "contract-api-key",
        "https://example.invalid/embeddings"
    );
    var job = await client.SubmitJobAsync(
        "stats",
        new Dictionary<string, object?> { ["library_id"] = "lib-contract" }
    );
    await client.SubmitJobAsync(
        "index",
        new Dictionary<string, object?>
        {
            ["library_id"] = "lib-contract",
            ["tags"] = new[] { "new", "featured" },
        }
    );
    await client.SubmitJobAsync(
        "index",
        new Dictionary<string, object?>
        {
            ["library_id"] = "lib-contract",
            ["tags"] = Array.Empty<string>(),
        }
    );
    await client.SubmitJobAsync(
        "index",
        new Dictionary<string, object?>
        {
            ["library_id"] = "lib-contract",
            ["tags"] = null,
        }
    );
    await client.SubmitJobAsync(
        "search",
        new Dictionary<string, object?>
        {
            ["text"] = "diagnostic query",
            ["show_low_confidence"] = true,
        }
    );
    await client.EstimateAutoTagsAsync(new AutoTagEstimateRequest
    {
        LibraryId = "lib-contract",
        Scope = AutoTagScopes.Untagged,
        Model = AutoTagModels.Flash,
        MaxImages = 120,
        MaxBudgetCny = 3.5m,
    });
    await AssertThrowsAsync<ArgumentException>(() => client.StartAutoTagAsync(
        new AutoTagRunRequest
        {
            LibraryId = "lib-contract",
            Scope = AutoTagScopes.Untagged,
            Model = AutoTagModels.Flash,
            MaxImages = 120,
            MaxBudgetCny = 3.5m,
            ExternalProcessingConfirmed = false,
        }
    ));
    await client.StartAutoTagAsync(new AutoTagRunRequest
    {
        LibraryId = "lib-contract",
        Scope = AutoTagScopes.LatestIndexRun,
        Model = AutoTagModels.Flash,
        MaxImages = 120,
        MaxBudgetCny = 3.5m,
        ExternalProcessingConfirmed = true,
    });
    await AssertThrowsAsync<ArgumentOutOfRangeException>(() =>
        client.GetPendingAutoTagsAsync(new AutoTagPendingRequest
        {
            LibraryId = "lib-contract",
            Offset = -1,
            Limit = 100,
        })
    );
    await AssertThrowsAsync<ArgumentOutOfRangeException>(() =>
        client.GetPendingAutoTagsAsync(new AutoTagPendingRequest
        {
            LibraryId = "lib-contract",
            Limit = 501,
        })
    );
    await client.GetPendingAutoTagsAsync(new AutoTagPendingRequest
    {
        LibraryId = "lib-contract",
        Offset = 100,
        Limit = 100,
        Filters = new AutoTagReviewFilters
        {
            LatestIndexOnly = true,
            Character = "刻晴",
            ReviewState = "identity",
        },
    });
    await client.SubmitAutoTagReviewAsync(new AutoTagReviewRequest
    {
        LibraryId = "lib-contract",
        Decisions =
        [
            new AutoTagReviewDecision
            {
                ProposalId = "proposal-001",
                Decision = "accept",
                AcceptedTags = ["原神", "刻晴"],
                ConfirmedIdentityTags = ["原神", "刻晴"],
            },
        ],
    });
    await client.BackfillFolderTagsAsync(new FolderTagBackfillRequest
    {
        LibraryId = "lib-contract",
        Recursive = true,
        VerifyHash = false,
    });
    await AssertThrowsAsync<ArgumentOutOfRangeException>(() =>
        client.BackfillMetadataEmbeddingsAsync(new MetadataBackfillRequest
        {
            LibraryId = "lib-contract",
            MaxImages = 0,
        })
    );
    await client.BackfillMetadataEmbeddingsAsync(new MetadataBackfillRequest
    {
        LibraryId = "lib-contract",
        MaxImages = 275,
    });
    await AssertThrowsAsync<ArgumentException>(() => client.StartIndexAndAutoTagAsync(
        new IndexAndAutoTagRequest
        {
            LibraryId = "lib-contract",
            Tags = null,
            Model = AutoTagModels.Flash,
            MaxImages = 120,
            MaxBudgetCny = 3.5m,
            ExternalProcessingConfirmed = false,
        }
    ));
    await client.StartIndexAndAutoTagAsync(new IndexAndAutoTagRequest
    {
        LibraryId = "lib-contract",
        Recursive = true,
        VerifyHash = true,
        Tags = ["new", "featured"],
        Model = AutoTagModels.Flash,
        MaxImages = 120,
        MaxBudgetCny = 3.5m,
        ExternalProcessingConfirmed = true,
    });
    Assert(job.Status == "queued", "submitted job status");
    Assert(handler.Requests.Count == 13, "captured JSON request count");

    foreach (var captured in handler.Requests)
    {
        Assert(captured.ContentLength is > 0, $"{captured.Method} content length");
        Assert(
            captured.ContentLength == captured.Body.Length,
            $"{captured.Method} content length matches body"
        );
        Assert(!captured.UsedChunkedTransfer, $"{captured.Method} is not chunked");
        Assert(
            string.Equals(
                captured.ContentType,
                "application/json",
                StringComparison.OrdinalIgnoreCase
            ),
            $"{captured.Method} JSON content type"
        );
        using var document = JsonDocument.Parse(captured.Body);
        Assert(document.RootElement.ValueKind == JsonValueKind.Object, "JSON body object");
    }

    Assert(handler.Requests[0].Method == "PUT", "credentials request method");
    using (var credentials = JsonDocument.Parse(handler.Requests[0].Body))
    {
        var root = credentials.RootElement;
        Assert(
            root.GetProperty("dashscope_api_key").GetString() == "contract-api-key",
            "credentials API key body"
        );
        Assert(
            root.GetProperty("api_url").GetString() ==
                "https://example.invalid/embeddings",
            "credentials API URL body"
        );
    }

    Assert(handler.Requests[1].Method == "POST", "job request method");
    using (var jobRequest = JsonDocument.Parse(handler.Requests[1].Body))
    {
        var root = jobRequest.RootElement;
        Assert(root.GetProperty("command").GetString() == "stats", "job command body");
        Assert(
            root.GetProperty("params").GetProperty("library_id").GetString() ==
                "lib-contract",
            "job library body"
        );
    }

    using (var taggedIndex = JsonDocument.Parse(handler.Requests[2].Body))
    {
        var root = taggedIndex.RootElement;
        Assert(root.GetProperty("command").GetString() == "index", "tagged index command");
        var tags = root.GetProperty("params").GetProperty("tags");
        Assert(tags.ValueKind == JsonValueKind.Array, "tagged index tags array");
        Assert(tags.GetArrayLength() == 2, "tagged index tags count");
        Assert(tags[0].GetString() == "new", "tagged index first tag");
        Assert(tags[1].GetString() == "featured", "tagged index second tag");
    }

    using (var clearIndex = JsonDocument.Parse(handler.Requests[3].Body))
    {
        var tags = clearIndex.RootElement.GetProperty("params").GetProperty("tags");
        Assert(tags.ValueKind == JsonValueKind.Array, "clear index tags array");
        Assert(tags.GetArrayLength() == 0, "clear index preserves empty-array intent");
    }

    using (var unchangedIndex = JsonDocument.Parse(handler.Requests[4].Body))
    {
        var tags = unchangedIndex.RootElement.GetProperty("params").GetProperty("tags");
        Assert(tags.ValueKind == JsonValueKind.Null, "unchanged index preserves null intent");
    }

    using (var lowConfidenceSearch = JsonDocument.Parse(handler.Requests[5].Body))
    {
        var root = lowConfidenceSearch.RootElement;
        Assert(root.GetProperty("command").GetString() == "search", "diagnostic search command");
        Assert(
            root.GetProperty("params").GetProperty("show_low_confidence").GetBoolean(),
            "diagnostic search override body"
        );
    }

    using (var estimate = JsonDocument.Parse(handler.Requests[6].Body))
    {
        var root = estimate.RootElement;
        var parameters = root.GetProperty("params");
        Assert(
            root.GetProperty("command").GetString() == "auto_tag_estimate",
            "auto-tag estimate command"
        );
        Assert(
            parameters.GetProperty("scope").GetString() == "untagged",
            "auto-tag estimate scope"
        );
        Assert(parameters.GetProperty("max_images").GetInt32() == 120, "auto-tag image cap");
        Assert(
            parameters.GetProperty("max_budget_cny").GetDecimal() == 3.5m,
            "auto-tag budget cap"
        );
        Assert(
            !parameters.GetProperty("external_processing_confirmed").GetBoolean(),
            "estimate does not imply external-processing consent"
        );
    }

    using (var autoTag = JsonDocument.Parse(handler.Requests[7].Body))
    {
        var root = autoTag.RootElement;
        var parameters = root.GetProperty("params");
        Assert(root.GetProperty("command").GetString() == "auto_tag", "auto-tag command");
        Assert(
            parameters.GetProperty("model").GetString() == "qwen3-vl-flash",
            "auto-tag model"
        );
        Assert(
            parameters.GetProperty("scope").GetString() == "latest_index_run",
            "auto-tag latest-index scope"
        );
        Assert(
            parameters.GetProperty("external_processing_confirmed").GetBoolean(),
            "auto-tag explicit external-processing consent"
        );
    }

    using (var pending = JsonDocument.Parse(handler.Requests[8].Body))
    {
        var root = pending.RootElement;
        var parameters = root.GetProperty("params");
        Assert(
            root.GetProperty("command").GetString() == "auto_tag_pending",
            "pending auto-tag command"
        );
        Assert(parameters.GetProperty("library_id").GetString() == "lib-contract", "pending library");
        Assert(parameters.GetProperty("offset").GetInt32() == 100, "pending offset");
        Assert(parameters.GetProperty("limit").GetInt32() == 100, "pending limit");
        var filters = parameters.GetProperty("filters");
        Assert(filters.GetProperty("latest_index_only").GetBoolean(), "pending latest-index filter");
        Assert(filters.GetProperty("character").GetString() == "刻晴", "pending character filter");
        Assert(filters.GetProperty("review_state").GetString() == "identity", "pending review-state filter");
    }

    using (var review = JsonDocument.Parse(handler.Requests[9].Body))
    {
        var root = review.RootElement;
        var parameters = root.GetProperty("params");
        Assert(
            root.GetProperty("command").GetString() == "auto_tag_review",
            "auto-tag review command"
        );
        var decision = parameters.GetProperty("decisions")[0];
        Assert(decision.GetProperty("proposal_id").GetString() == "proposal-001", "proposal ID");
        Assert(decision.GetProperty("decision").GetString() == "accept", "review decision");
        Assert(
            decision.GetProperty("accepted_tags")[1].GetString() == "刻晴",
            "review accepted tags"
        );
        Assert(
            decision.GetProperty("confirmed_identity_tags")[0].GetString() == "原神",
            "review confirmed identity tags"
        );
    }

    using (var backfill = JsonDocument.Parse(handler.Requests[10].Body))
    {
        var root = backfill.RootElement;
        var parameters = root.GetProperty("params");
        Assert(
            root.GetProperty("command").GetString() == "folder_tag_backfill",
            "folder-tag backfill command"
        );
        Assert(parameters.GetProperty("recursive").GetBoolean(), "folder-tag recursive flag");
        Assert(!parameters.GetProperty("verify_hash").GetBoolean(), "folder-tag hash flag");
    }
    using (var metadataBackfill = JsonDocument.Parse(handler.Requests[11].Body))
    {
        var root = metadataBackfill.RootElement;
        var parameters = root.GetProperty("params");
        Assert(
            root.GetProperty("command").GetString() == "metadata_backfill",
            "metadata embedding backfill command"
        );
        Assert(
            parameters.GetProperty("library_id").GetString() == "lib-contract",
            "metadata backfill library"
        );
        Assert(
            parameters.GetProperty("max_images").GetInt32() == 275,
            "metadata backfill image cap"
        );
    }
    using (var combined = JsonDocument.Parse(handler.Requests[12].Body))
    {
        var root = combined.RootElement;
        var parameters = root.GetProperty("params");
        Assert(
            root.GetProperty("command").GetString() == "index_and_auto_tag",
            "combined index and auto-tag command"
        );
        Assert(parameters.GetProperty("library_id").GetString() == "lib-contract", "combined library");
        Assert(parameters.GetProperty("recursive").GetBoolean(), "combined recursive flag");
        Assert(parameters.GetProperty("verify_hash").GetBoolean(), "combined hash flag");
        Assert(parameters.GetProperty("tags").GetArrayLength() == 2, "combined tags");
        Assert(parameters.GetProperty("model").GetString() == AutoTagModels.Flash, "combined model");
        Assert(parameters.GetProperty("max_images").GetInt32() == 120, "combined max images");
        Assert(parameters.GetProperty("max_budget_cny").GetDecimal() == 3.5m, "combined budget");
        Assert(
            parameters.GetProperty("external_processing_confirmed").GetBoolean(),
            "combined explicit external-processing consent"
        );
        Assert(!parameters.TryGetProperty("scope", out _), "combined scope is fixed by the pipeline");
        Assert(!parameters.TryGetProperty("folder", out _), "combined folder remains optional");
    }

    var nullTags = new IndexAndAutoTagRequest
    {
        LibraryId = "lib-contract",
        Tags = null,
        ExternalProcessingConfirmed = true,
    }.ToParameters();
    var clearTags = new IndexAndAutoTagRequest
    {
        LibraryId = "lib-contract",
        Tags = Array.Empty<string>(),
        ExternalProcessingConfirmed = true,
    }.ToParameters();
    Assert(nullTags["tags"] is null, "combined null tags preserve no-change intent");
    Assert(
        clearTags["tags"] is IReadOnlyList<string> { Count: 0 },
        "combined empty tags preserve clear intent"
    );
}

static async Task TestAutoTagReviewWorkbenchApiContractsAsync()
{
    using var handler = new CapturingJsonHandler();
    using var httpClient = new HttpClient(handler);
    using var client = new BackendApiClient(
        new Uri("http://127.0.0.1:8765/"),
        "review-workbench-token",
        httpClient
    );

    await AssertThrowsAsync<ArgumentException>(() => client.SubmitAutoTagBatchReviewAsync(
        new AutoTagBatchReviewRequest
        {
            LibraryId = "library-a",
            ProposalIds = ["proposal-001"],
            AcceptedTagsByProposal = new Dictionary<string, IReadOnlyList<string>>
            {
                ["proposal-001"] = ["站姿"],
            },
            ExcludeIdentityTags = false,
        }
    ));
    await client.SubmitAutoTagBatchReviewAsync(new AutoTagBatchReviewRequest
    {
        LibraryId = "library-a",
        ProposalIds = ["proposal-001"],
        AcceptedTagsByProposal = new Dictionary<string, IReadOnlyList<string>>
        {
            ["proposal-001"] = ["站姿"],
        },
    });
    await client.UndoAutoTagBatchReviewAsync(new AutoTagReviewUndoRequest
    {
        LibraryId = "library-a",
    });
    await client.ListTagAliasesAsync(new TagAliasListRequest
    {
        LibraryId = "library-a",
    });
    await client.UpsertTagAliasAsync(new TagAliasUpsertRequest
    {
        LibraryId = "library-a",
        CanonicalName = "雷电将军",
        Aliases = ["雷神", "影"],
    });
    await client.DeleteTagAliasAsync(new TagAliasDeleteRequest
    {
        LibraryId = "library-a",
        CanonicalName = "雷电将军",
    });

    Assert(handler.Requests.Count == 5, "review workbench API request count");
    foreach (var captured in handler.Requests)
    {
        Assert(captured.ContentLength == captured.Body.Length, "workbench request content length");
        Assert(!captured.UsedChunkedTransfer, "workbench request is not chunked");
    }

    using (var batch = JsonDocument.Parse(handler.Requests[0].Body))
    {
        var root = batch.RootElement;
        Assert(root.GetProperty("command").GetString() == "auto_tag_review_batch", "batch command");
        var parameters = root.GetProperty("params");
        Assert(parameters.GetProperty("exclude_identity_tags").GetBoolean(), "batch excludes identity");
        Assert(parameters.GetProperty("proposal_ids")[0].GetString() == "proposal-001", "batch proposal");
        Assert(
            parameters.GetProperty("accepted_tags_by_proposal")
                .GetProperty("proposal-001")[0]
                .GetString() == "站姿",
            "batch accepted low-risk tags"
        );
    }
    using (var undo = JsonDocument.Parse(handler.Requests[1].Body))
    {
        Assert(
            undo.RootElement.GetProperty("command").GetString() == "auto_tag_review_undo",
            "undo command"
        );
    }
    using (var list = JsonDocument.Parse(handler.Requests[2].Body))
    {
        Assert(list.RootElement.GetProperty("command").GetString() == "tag_alias_list", "alias list command");
    }
    using (var upsert = JsonDocument.Parse(handler.Requests[3].Body))
    {
        var root = upsert.RootElement;
        Assert(root.GetProperty("command").GetString() == "tag_alias_upsert", "alias upsert command");
        Assert(
            root.GetProperty("params").GetProperty("canonical_name").GetString() == "雷电将军",
            "alias canonical name"
        );
    }
    using (var delete = JsonDocument.Parse(handler.Requests[4].Body))
    {
        Assert(delete.RootElement.GetProperty("command").GetString() == "tag_alias_delete", "alias delete command");
    }
}

static async Task TestSearchResultQualityPresentationAsync()
{
    var root = Path.Combine(
        Path.GetTempPath(),
        $"zvec-search-results-contract-{Guid.NewGuid():N}"
    );
    var resultsRoot = Path.Combine(root, "results");
    var imageRoot = Path.Combine(root, "images");
    try
    {
        Directory.CreateDirectory(resultsRoot);
        Directory.CreateDirectory(imageRoot);
        var config = new LauncherConfig
        {
            SchemaVersion = LauncherConfig.CurrentSchemaVersion,
            ResultsDirectory = resultsRoot,
            DefaultLibraryId = "library-a",
            Libraries =
            [
                new LauncherLibrary
                {
                    Id = "library-a",
                    Name = "图库 A",
                    ImageRoot = imageRoot,
                    WorkspaceDirectory = Path.Combine(root, "workspace-a"),
                },
                new LauncherLibrary
                {
                    Id = "library-b",
                    Name = "图库 B",
                    ImageRoot = imageRoot,
                    WorkspaceDirectory = Path.Combine(root, "workspace-b"),
                },
            ],
        };
        var loader = new SearchResultLoader();

        var qualityDirectory = Directory.CreateDirectory(
            Path.Combine(resultsRoot, "quality-session")
        ).FullName;
        var qualityManifest = new
        {
            status = "ok",
            candidate_count = 50,
            filtered_count = 47,
            latency_ms = 123.4,
            ranking_mode = "confidence_v2",
            query_type = "image_text",
            output_dir = "/data/results/quality-session",
            result_count = 1,
            library_ids = new[] { "library-a", "library-b" },
            library_names = new[] { "图库 A", "图库 B" },
            query = new
            {
                text = "海边日落",
                image = "query.jpg",
                tags = new[] { "旅行" },
            },
            results = new[]
            {
                new
                {
                    rank = 1,
                    distance = 0.08,
                    fused_score = 0.94,
                    raw_score = 0.94,
                    normalized_score = 0.96,
                    confidence = 0.94,
                    ranking_confidence = 0.97,
                    match_state = "high",
                    rank_source = "fused",
                    image_confidence = 0.95,
                    text_confidence = 0.90,
                    metadata_confidence = 0.88,
                    image_rank = 1,
                    text_rank = 2,
                    metadata_rank = 3,
                    rank_agreement = 0.951,
                    library_id = "library-a",
                    library_name = "图库 A",
                    root_id = "root-a",
                    relative_path = "photos/sunset.jpg",
                    sha256 = new string('a', 64),
                    copied_file = "001_confidence_0.940000_sunset.jpg",
                     doc_id = "sunset-doc",
                     tags = new[] { "旅行", "日落" },
                     matched_tags = new[] { "旅行" },
                },
            },
            copy_failures = Array.Empty<object>(),
        };
        await WriteManifestAsync(qualityDirectory, qualityManifest);
        var quality = await loader.LoadFromCommandOutputAsync(
            config,
            "搜索完成\n{\"output_dir\":\"/data/results/quality-session\"}"
        ) ?? throw new Exception("Quality search manifest was not loaded.");

        Assert(quality.Items.Count == 1, "dynamic result count");
        Assert(quality.Manifest.CandidateCount == 50, "candidate count mapped");
        Assert(quality.Manifest.FilteredCount == 47, "filtered count mapped");
        Assert(quality.Summary.Contains("2 个图库", StringComparison.Ordinal), "searched library count");
        Assert(quality.Summary.Contains("1 个可信结果", StringComparison.Ordinal), "calibrated result summary");
        Assert(quality.Summary.Contains("候选 50", StringComparison.Ordinal), "candidate summary");
        Assert(quality.Summary.Contains("123 ms", StringComparison.Ordinal), "search latency summary");
        var item = quality.Items[0];
        Assert(item.RawScore == 0.94, "raw score mapped");
        Assert(item.NormalizedScore == 0.96, "normalized score mapped");
        Assert(item.Confidence == 0.94, "confidence mapped");
        Assert(item.RankingConfidence == 0.97, "ranking confidence mapped");
        Assert(item.MatchStateText == "高度相关", "match state presented");
        Assert(item.RankSourceText == "图文联合", "rank source presented");
        Assert(item.ImageConfidence == 0.95 && item.ImageRank == 1, "image channel diagnostics");
        Assert(item.TextConfidence == 0.90 && item.TextRank == 2, "text channel diagnostics");
        Assert(
            item.MetadataConfidence == 0.88 && item.MetadataRank == 3,
            "metadata channel diagnostics"
        );
        Assert(item.RankAgreement == 0.951, "rank agreement mapped");
        Assert(item.MatchedTags.SequenceEqual(["旅行"]), "matched tags mapped");
        Assert(item.MatchedTagsText == "命中标签：旅行", "matched tags are presented");
        Assert(item.DiagnosticScoreText.Contains("图片", StringComparison.Ordinal), "image diagnostics presented");
        Assert(item.DiagnosticScoreText.Contains("文字", StringComparison.Ordinal), "text diagnostics presented");
        Assert(
            item.DiagnosticScoreText.Contains("描述 0.880 #3", StringComparison.Ordinal),
            "metadata diagnostics presented"
        );
        Assert(item.DiagnosticScoreText.Contains("通道一致度", StringComparison.Ordinal), "agreement presented");
        Assert(item.DiagnosticScoreText.Contains("原始 0.940000", StringComparison.Ordinal), "raw score diagnostics presented");
        Assert(item.DiagnosticScoreText.Contains("排序置信度 0.970", StringComparison.Ordinal), "ranking confidence diagnostics presented");
        Assert(!item.DiagnosticScoreText.Contains('%'), "diagnostic scores are not percentages");

        var presentationCases = new[]
        {
            (State: "high", StateText: "高度相关", Source: "fused", SourceText: "图文联合"),
            (State: "possible", StateText: "可能相关", Source: "image", SourceText: "图片匹配"),
            (State: "weak", StateText: "低置信度", Source: "text", SourceText: "文字匹配"),
            (State: "possible", StateText: "可能相关", Source: "metadata", SourceText: "描述匹配"),
            (State: "high", StateText: "高度相关", Source: "tag", SourceText: "标签匹配"),
        };
        foreach (var presentationCase in presentationCases)
        {
            var presentationItem = CreatePresentationItem(
                presentationCase.State,
                presentationCase.Source
            );
            Assert(
                presentationItem.MatchStateText == presentationCase.StateText,
                $"{presentationCase.State} match state presented"
            );
            Assert(
                presentationItem.RankSourceText == presentationCase.SourceText,
                $"{presentationCase.Source} rank source presented"
            );
            Assert(
                presentationItem.ScoreText.Contains(presentationCase.StateText, StringComparison.Ordinal),
                $"{presentationCase.State} appears in result label"
            );
            Assert(
                !presentationItem.ScoreText.Contains('%') &&
                    !presentationItem.DiagnosticScoreText.Contains('%'),
                $"{presentationCase.State} is not rendered as an uncalibrated percentage"
            );
        }

        var emptyDirectory = Directory.CreateDirectory(
            Path.Combine(resultsRoot, "empty-session")
        ).FullName;
        var emptyManifest = new
        {
            status = "no_reliable_match",
            candidate_count = 50,
            filtered_count = 50,
            latency_ms = 88.0,
            ranking_mode = "confidence_v2",
            query_type = "text",
            output_dir = "/data/results/empty-session",
            result_count = 0,
            library_ids = Array.Empty<string>(),
            library_names = Array.Empty<string>(),
            libraries = new[]
            {
                new { id = "library-a", name = "图库 A" },
                new { id = "library-b", name = "图库 B" },
            },
            query = new { text = "图库里不存在的对象", tags = new[] { "排除项" } },
            results = Array.Empty<object>(),
            copy_failures = Array.Empty<object>(),
        };
        await WriteManifestAsync(emptyDirectory, emptyManifest);
        var empty = await loader.LoadFromCommandOutputAsync(
            config,
            "{\"output_dir\":\"/data/results/empty-session\"}"
        ) ?? throw new Exception("No-match search manifest was not loaded.");

        Assert(empty.IsNoReliableMatch, "no reliable match state");
        Assert(empty.EmptyTitle == "无可靠结果", "no-match title");
        Assert(empty.Summary.Contains("2 个图库", StringComparison.Ordinal), "no-match searched libraries");
        Assert(empty.Summary.Contains("无可靠结果", StringComparison.Ordinal), "no-match summary");
        Assert(empty.EmptyCaption.Contains("50 个候选", StringComparison.Ordinal), "no-match candidate diagnostics");
        Assert(empty.EmptyCaption.Contains("图库 A、图库 B", StringComparison.Ordinal), "no-match library diagnostics");
        Assert(empty.EmptyCaption.Contains("标签筛选已启用", StringComparison.Ordinal), "no-match tag diagnostics");
        Assert(empty.EmptyCaption.Contains("耗时 88 ms", StringComparison.Ordinal), "no-match latency diagnostics");
        Assert(empty.CanShowLowConfidence, "no-match can offer low-confidence override");
        Assert(!empty.CanOfferLowConfidence(false), "override stays hidden before capability detection");
        Assert(empty.CanOfferLowConfidence(true), "override becomes available after capability detection");

        var overrideDirectory = Directory.CreateDirectory(
            Path.Combine(resultsRoot, "override-session")
        ).FullName;
        var overrideManifest = new
        {
            status = "low_confidence_override",
            show_low_confidence = true,
            low_confidence_override = true,
            candidate_count = 50,
            filtered_count = 49,
            latency_ms = 91.0,
            ranking_mode = "confidence_v2",
            query_type = "text",
            output_dir = "/data/results/override-session",
            result_count = 1,
            library_ids = new[] { "library-a", "library-b" },
            library_names = new[] { "图库 A", "图库 B" },
            query = new
            {
                text = "图库里不存在的对象",
                tags = Array.Empty<string>(),
                show_low_confidence = true,
            },
            results = new[]
            {
                new
                {
                    rank = 1,
                    distance = 0.8,
                    raw_score = 0.8,
                    normalized_score = 0.6,
                    confidence = 0.2,
                    match_state = "weak",
                    rank_source = "text",
                    library_id = "library-a",
                    library_name = "图库 A",
                    root_id = "root-a",
                    relative_path = "photos/weak.jpg",
                    copied_file = "001_distance_0.800000_weak.jpg",
                    doc_id = "weak-doc",
                    tags = Array.Empty<string>(),
                },
            },
            copy_failures = Array.Empty<object>(),
        };
        await WriteManifestAsync(overrideDirectory, overrideManifest);
        var lowConfidence = await loader.LoadFromCommandOutputAsync(
            config,
            "{\"output_dir\":\"/data/results/override-session\"}"
        ) ?? throw new Exception("Low-confidence override manifest was not loaded.");

        Assert(lowConfidence.IsLowConfidenceOverride, "override state mapped");
        Assert(!lowConfidence.CanShowLowConfidence, "override is not offered twice");
        Assert(
            lowConfidence.Summary.Contains("低置信候选（不可信）", StringComparison.Ordinal),
            "override summary warns results are untrusted"
        );
        Assert(
            lowConfidence.Items[0].ScoreText.Contains("不可信候选", StringComparison.Ordinal),
            "override result is explicitly untrusted"
        );
        Assert(
            lowConfidence.Items[0].ScoreText.Contains("低置信度", StringComparison.Ordinal),
            "override result retains weak match state"
        );
        Assert(
            lowConfidence.Items[0].DiagnosticScoreText.Contains(
                "低置信展示已开启",
                StringComparison.Ordinal
            ),
            "override diagnostics are visible"
        );

        var copyFailureDirectory = Directory.CreateDirectory(
            Path.Combine(resultsRoot, "override-copy-failure-session")
        ).FullName;
        var copyFailureManifest = new
        {
            status = "low_confidence_override",
            show_low_confidence = true,
            low_confidence_override = true,
            candidate_count = 1,
            filtered_count = 0,
            ranking_mode = "confidence",
            query_type = "text",
            output_dir = "/data/results/override-copy-failure-session",
            result_count = 0,
            library_ids = new[] { "library-a" },
            library_names = new[] { "图库 A" },
            query = new { text = "missing file", show_low_confidence = true },
            results = Array.Empty<object>(),
            copy_failures = new[]
            {
                new { path = "photos/missing.jpg", error = "source image no longer exists" },
            },
        };
        await WriteManifestAsync(copyFailureDirectory, copyFailureManifest);
        var copyFailure = await loader.LoadFromCommandOutputAsync(
            config,
            "{\"output_dir\":\"/data/results/override-copy-failure-session\"}"
        ) ?? throw new Exception("Override copy-failure manifest was not loaded.");
        Assert(copyFailure.Items.Count == 0, "override copy failure has no items");
        Assert(copyFailure.EmptyTitle == "仍无可显示候选", "override empty title");
        Assert(
            copyFailure.EmptyCaption.Contains("1 个候选复制失败", StringComparison.Ordinal),
            "override copy failure is explained"
        );

        var legacyDirectory = Directory.CreateDirectory(
            Path.Combine(resultsRoot, "legacy-session")
        ).FullName;
        var legacyManifest = new
        {
            query_type = "text",
            output_dir = "/data/results/legacy-session",
            result_count = 1,
            query = new { text = "旧格式" },
            results = new[]
            {
                new
                {
                    rank = 1,
                    distance = 0.2,
                    root_id = "root-a",
                    relative_path = "legacy.jpg",
                    copied_file = "001_distance_0.200000_legacy.jpg",
                    doc_id = "legacy-doc",
                    tags = Array.Empty<string>(),
                },
            },
            copy_failures = Array.Empty<object>(),
        };
        await WriteManifestAsync(legacyDirectory, legacyManifest);
        var legacy = await loader.LoadFromCommandOutputAsync(
            config,
            "{\"output_dir\":\"/data/results/legacy-session\"}"
        ) ?? throw new Exception("Legacy search manifest was not loaded.");

        Assert(legacy.Items.Count == 1, "legacy result loaded");
        Assert(legacy.Summary.Contains("1 个结果", StringComparison.Ordinal), "legacy summary is not overclaimed");
        Assert(!legacy.Summary.Contains("可信结果", StringComparison.Ordinal), "legacy result not called calibrated");
        Assert(legacy.Items[0].ScoreText.Contains("相关性未校准", StringComparison.Ordinal), "legacy score compatibility");
        Assert(legacy.Items[0].DiagnosticScoreText.Contains("旧版距离", StringComparison.Ordinal), "legacy diagnostics fallback");

        var noCandidate = new SearchResultSession
        {
            Manifest = new SearchManifest
            {
                Status = "no_reliable_match",
                RankingMode = "confidence",
                QueryType = "text",
                LibraryNames = ["图库 A"],
                Query = JsonSerializer.SerializeToElement(new { text = "empty" }),
            },
            Directory = resultsRoot,
            ManifestPath = string.Empty,
            Items = [],
        };
        Assert(
            noCandidate.EmptyCaption.Contains("没有召回到候选", StringComparison.Ordinal),
            "zero-candidate state does not claim threshold rejection"
        );
        Assert(!noCandidate.CanShowLowConfidence, "zero-candidate state cannot offer override");
    }
    finally
    {
        if (Directory.Exists(root))
        {
            Directory.Delete(root, recursive: true);
        }
    }
}

static SearchResultItem CreatePresentationItem(
    string matchState,
    string rankSource,
    bool isLowConfidenceOverride = false)
{
    return new SearchResultItem
    {
        Rank = 1,
        Name = "result.jpg",
        RelativePath = "photos/result.jpg",
        CopiedPath = "result.jpg",
        DocumentId = "result-doc",
        LibraryId = "library-a",
        LibraryName = "图库 A",
        RootId = "root-a",
        Distance = 0.25,
        RawScore = 0.25,
        NormalizedScore = 0.75,
        Confidence = 0.75,
        MatchState = matchState,
        RankSource = rankSource,
        IsLowConfidenceOverride = isLowConfidenceOverride,
        Tags = [],
    };
}

static async Task WriteManifestAsync<T>(string directory, T manifest)
{
    await File.WriteAllTextAsync(
        Path.Combine(directory, "results.json"),
        JsonSerializer.Serialize(manifest),
        new UTF8Encoding(encoderShouldEmitUTF8Identifier: false)
    );
}

static void Assert(bool condition, string contract)
{
    if (!condition)
    {
        throw new Exception($"Contract failed: {contract}");
    }
}

static async Task AssertThrowsAsync<TException>(Func<Task> callback)
    where TException : Exception
{
    try
    {
        await callback();
    }
    catch (TException)
    {
        return;
    }
    throw new Exception($"Expected {typeof(TException).Name} was not thrown.");
}

static bool TryCreateFileSymbolicLink(string linkPath, string targetPath)
{
    try
    {
        File.CreateSymbolicLink(linkPath, targetPath);
        return true;
    }
    catch (Exception exception) when (
        exception is IOException or UnauthorizedAccessException or NotSupportedException
    )
    {
        // Some Windows hosts disable unprivileged symbolic-link creation. Production
        // rejection is still covered wherever the platform permits creating the fixture.
        return false;
    }
}

static async Task TestBackendJobListContractsAsync()
{
    const string payload =
        """
        {
          "jobs": [
            {
              "id": "11111111111111111111111111111111",
              "command": "index",
              "status": "partial",
              "failure_count": 3,
              "progress": {"message": "Completed with item failures.", "failed": 3}
            },
            {
              "id": "22222222222222222222222222222222",
              "command": "auto_tag",
              "status": "paused"
            },
            {
              "id": "33333333333333333333333333333333",
              "command": "index",
              "status": "needs_attention"
            }
          ],
          "count": 3,
          "total_count": 8
        }
        """;
    var handler = new CapturingGetHandler(payload);
    using var httpClient = new HttpClient(handler);
    using var client = new BackendApiClient(
        new Uri("http://127.0.0.1:8765/"),
        "contract-token",
        httpClient
    );

    var jobs = await client.GetJobsAsync(active: true, limit: 25);
    Assert(jobs.Count == 3 && jobs.TotalCount == 8, "job-list counts");
    Assert(jobs.Jobs[0].IsPartial, "partial job compatibility");
    Assert(jobs.Jobs[0].FailureCount == 3, "partial failure count");
    Assert(jobs.Jobs[1].IsPaused, "paused job compatibility");
    Assert(
        jobs.Jobs[2].NeedsAttention && jobs.Jobs[2].IsTerminal,
        "needs-attention job compatibility"
    );
    var taskItem = new BackendTaskItem();
    taskItem.Update(jobs.Jobs[0], "正在建立索引…");
    Assert(taskItem.Title == "建立索引", "task-center command title");
    Assert(taskItem.HasFailures && !taskItem.CanCancel, "partial task-center state");
    Assert(!taskItem.CanOpenFailureDirectory, "missing failure path hides directory action");
    taskItem.Update(new BackendJob
    {
        Id = "44444444444444444444444444444444",
        Command = "auto_tag",
        Status = "running",
        Progress = new BackendJobProgress
        {
            Message = "Auto-tagged 40/100 unique images.",
            Current = 40,
            Total = 100,
        },
    });
    Assert(taskItem.CanCancel, "running task can be independently cancelled");
    Assert(taskItem.ProgressValue == 40 && taskItem.ProgressMaximum == 100, "task progress");
    taskItem.Update(new BackendJob
    {
        Id = "45454545454545454545454545454545",
        Command = "index_and_auto_tag",
        Status = "queued",
    });
    Assert(taskItem.Title == "索引并智能标注", "combined task-center title");
    var failureTestRoot = Path.Combine(
        Path.GetTempPath(),
        $"zvec-failure-directory-contract-{Guid.NewGuid():N}"
    );
    try
    {
        var failedImages = Path.Combine(failureTestRoot, "failed-images");
        var jobsDirectory = Path.Combine(failedImages, "jobs");
        Directory.CreateDirectory(jobsDirectory);
        var manifest = Path.Combine(jobsDirectory, "job-001.jsonl");
        await File.WriteAllTextAsync(manifest, "{}\n");
        using var resultDocument = JsonDocument.Parse(JsonSerializer.Serialize(new
        {
            failure_manifest = manifest,
            quarantined = 2,
        }));
        taskItem.Update(new BackendJob
        {
            Id = "55555555555555555555555555555555",
            Command = "index",
            Status = "partial",
            FailureCount = 2,
            Result = resultDocument.RootElement.Clone(),
        });
        Assert(taskItem.CanOpenFailureDirectory, "failure-directory task action");
        Assert(
            FailureDirectoryResolver.TryResolve(
                failureTestRoot,
                taskItem.FailureManifestPath,
                taskItem.QuarantinedCount,
                out var resolvedFailureDirectory
            ) && resolvedFailureDirectory == failedImages,
            "failure manifest resolves only to the managed failed-images root"
        );
        var escapedManifest = Path.Combine(failureTestRoot, "outside.jsonl");
        await File.WriteAllTextAsync(escapedManifest, "{}\n");
        Assert(
            !FailureDirectoryResolver.TryResolve(
                failureTestRoot,
                escapedManifest,
                1,
                out _
            ),
            "failure-directory path escape is rejected"
        );
    }
    finally
    {
        Directory.Delete(failureTestRoot, recursive: true);
    }
    Assert(
        handler.LastRequestUri?.PathAndQuery == "/v1/jobs?active=true&limit=25",
        "job-list active query"
    );
    await AssertThrowsAsync<ArgumentOutOfRangeException>(() =>
        client.GetJobsAsync(limit: 501)
    );
}

static async Task TestBackendJobBatchMonitorAsync()
{
    const string recoveryPayload =
        """
        {
          "jobs": [
            {
              "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
              "command": "index",
              "status": "running",
              "progress": {"message": "older snapshot", "current": 1, "total": 10}
            },
            {
              "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
              "command": "index",
              "status": "running",
              "progress": {"message": "newer snapshot", "current": 2, "total": 10}
            },
            {
              "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
              "command": "search",
              "status": "succeeded"
            }
          ],
          "count": 3,
          "total_count": 2
        }
        """;
    const string refreshPayload =
        """
        {
          "jobs": [
            {
              "id": "cccccccccccccccccccccccccccccccc",
              "command": "index_and_auto_tag",
              "status": "queued"
            },
            {
              "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
              "command": "index",
              "status": "partial",
              "failure_count": 1
            },
            {
              "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
              "command": "search",
              "status": "succeeded"
            }
          ],
          "count": 3,
          "total_count": 3
        }
        """;

    using var handler = new SequenceJsonHandler(recoveryPayload, refreshPayload);
    using var httpClient = new HttpClient(handler);
    using var client = new BackendApiClient(
        new Uri("http://127.0.0.1:8765/"),
        "contract-token",
        httpClient
    );
    var updates = new List<BackendJobBatchUpdate>();
    var refreshed = new TaskCompletionSource(
        TaskCreationOptions.RunContinuationsAsynchronously
    );
    var monitor = new BackendJobBatchMonitor(
        cancellationToken => client.GetJobsAsync(
            active: null,
            limit: 50,
            cancellationToken: cancellationToken
        ),
        (update, _) =>
        {
            lock (updates)
            {
                updates.Add(update);
                if (updates.Count >= 2)
                {
                    refreshed.TrySetResult();
                }
            }
            return Task.CompletedTask;
        },
        TimeSpan.FromMilliseconds(15)
    );

    await monitor.RefreshOnceAsync(isRecovery: true);
    using var monitorCancellation = new CancellationTokenSource();
    var monitorTask = monitor.RunAsync(monitorCancellation.Token);
    await refreshed.Task.WaitAsync(TimeSpan.FromSeconds(2));
    monitorCancellation.Cancel();
    try
    {
        await monitorTask;
    }
    catch (OperationCanceledException) when (monitorCancellation.IsCancellationRequested)
    {
        // Cancelling the monitor is the expected window-close path.
    }

    BackendJobBatchUpdate[] snapshots;
    lock (updates)
    {
        snapshots = updates.ToArray();
    }
    Assert(snapshots.Length >= 2, "job monitor performs batch refresh");
    var recovery = snapshots[0];
    Assert(recovery.IsRecovery, "first job batch is marked as recovery");
    Assert(recovery.Jobs.Count == 2, "recovery batch deduplicates job IDs");
    Assert(recovery.AddedCount == 2 && recovery.ActiveCount == 1, "recovery counts");
    Assert(
        recovery.Jobs[0].Progress?.Current == 2,
        "duplicate recovery snapshots prefer the latest occurrence"
    );
    var refresh = snapshots[1];
    Assert(!refresh.IsRecovery, "subsequent job batch is a refresh");
    Assert(refresh.AddedCount == 1, "batch refresh recognizes one newly observed job");
    Assert(
        refresh.Jobs.Any(job => job.Command == "index_and_auto_tag" && !job.IsTerminal),
        "combined jobs are restored without command-specific replay"
    );
    var requests = handler.Requests;
    Assert(requests.Count >= 2, "job monitor uses repeated list requests");
    Assert(
        requests.All(request =>
            request.Method == HttpMethod.Get &&
            request.PathAndQuery == "/v1/jobs?limit=50"
        ),
        "stopping the monitor never sends a job-cancellation request"
    );
}

static async Task<TException> CaptureExceptionAsync<TException>(Func<Task> callback)
    where TException : Exception
{
    try
    {
        await callback();
    }
    catch (TException exception)
    {
        return exception;
    }
    throw new Exception($"Expected {typeof(TException).Name} was not thrown.");
}

internal static class BackendPayloadContractFixture
{
    public static async Task<string> CreateAsync(
        string directory,
        string marker = "baseline",
        bool reverseCreationOrder = false)
    {
        Directory.CreateDirectory(directory);
        var files = new (string RelativePath, string Content)[]
        {
            ("image_service.py", $"# image service: {marker}\n"),
            ("zvec_logging.py", $"# logging: {marker}\n"),
            ("requirements-lock.txt", $"# requirements: {marker}\n"),
            ("model-catalog.default.json", $"{{\"marker\":\"{marker}\"}}\n"),
            ("pyproject.toml", $"# project: {marker}\n"),
            (Path.Combine("image_vector_service", "__init__.py"), "# package\n"),
            (
                Path.Combine("image_vector_service", "backend_server.py"),
                $"# backend server: {marker}\n"
            ),
            (
                Path.Combine("image_vector_service", "nested", "worker.py"),
                $"# nested worker: {marker}\n"
            ),
        };
        if (reverseCreationOrder)
        {
            Array.Reverse(files);
        }
        foreach (var (relativePath, content) in files)
        {
            var fullPath = Path.Combine(directory, relativePath);
            Directory.CreateDirectory(Path.GetDirectoryName(fullPath)!);
            await File.WriteAllTextAsync(
                fullPath,
                content,
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false)
            );
        }
        return Path.GetFullPath(Path.Combine(directory, "image_service.py"));
    }
}

file sealed class StaticJsonHandler(string json) : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        var response = new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json"),
            RequestMessage = request,
        };
        return Task.FromResult(response);
    }
}

file sealed class StatusJsonHandler(HttpStatusCode statusCode, string json) : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return Task.FromResult(new HttpResponseMessage(statusCode)
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json"),
            RequestMessage = request,
        });
    }
}

file sealed class CapturingGetHandler(string json) : HttpMessageHandler
{
    public Uri? LastRequestUri { get; private set; }

    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        LastRequestUri = request.RequestUri;
        return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json"),
            RequestMessage = request,
        });
    }
}

file sealed class SequenceJsonHandler(params string[] payloads) : HttpMessageHandler
{
    private readonly object _sync = new();
    private readonly List<(HttpMethod Method, string PathAndQuery)> _requests = [];
    private int _responseIndex;

    public IReadOnlyList<(HttpMethod Method, string PathAndQuery)> Requests
    {
        get
        {
            lock (_sync)
            {
                return _requests.ToArray();
            }
        }
    }

    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        string payload;
        lock (_sync)
        {
            if (payloads.Length == 0)
            {
                throw new InvalidOperationException("At least one JSON response is required.");
            }
            _requests.Add((
                request.Method,
                request.RequestUri?.PathAndQuery ?? string.Empty
            ));
            payload = payloads[Math.Min(_responseIndex, payloads.Length - 1)];
            _responseIndex++;
        }
        return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(payload, Encoding.UTF8, "application/json"),
            RequestMessage = request,
        });
    }
}

file sealed record CapturedJsonRequest(
    string Method,
    long? ContentLength,
    string? ContentType,
    bool UsedChunkedTransfer,
    byte[] Body
);

file sealed class CapturingJsonHandler : HttpMessageHandler
{
    public List<CapturedJsonRequest> Requests { get; } = [];

    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        var body = request.Content is null
            ? []
            : await request.Content.ReadAsByteArrayAsync(cancellationToken);
        Requests.Add(new CapturedJsonRequest(
            request.Method.Method,
            request.Content?.Headers.ContentLength,
            request.Content?.Headers.ContentType?.MediaType,
            request.Headers.TransferEncodingChunked == true,
            body
        ));

        var responseJson = request.RequestUri?.AbsolutePath switch
        {
            "/v1/session/credentials" => """{"credentials_configured":true}""",
            "/v1/jobs" =>
                """
                {"job":{"id":"0123456789abcdef0123456789abcdef","command":"stats","status":"queued"}}
                """,
            _ => "{}",
        };
        return new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(responseJson, Encoding.UTF8, "application/json"),
            RequestMessage = request,
        };
    }
}

file sealed class FakeRuntimeBootstrapCommandRunner(
    Func<string, CommandResult> responseFactory) : IRuntimeBootstrapCommandRunner, IDisposable
{
    public bool IsDisposed { get; private set; }

    public string? LastCommand { get; private set; }

    public IReadOnlyDictionary<string, string?>? LastEnvironment { get; private set; }

    public Task<CommandResult> RunZvecAsync(
        string command,
        IEnumerable<string>? arguments,
        IReadOnlyDictionary<string, string?>? environment,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        LastCommand = command;
        LastEnvironment = environment;
        if (arguments is not null)
        {
            throw new InvalidOperationException(
                "runtime bootstrap passes no launcher arguments"
            );
        }
        return Task.FromResult(responseFactory(command));
    }

    public void Dispose()
    {
        IsDisposed = true;
    }
}

file sealed class FakeWorkspaceCommandRunner(CommandResult result) : IZvecCommandRunner
{
    public CommandResult Result { get; set; } = result;

    public string? LastCommand { get; private set; }

    public IReadOnlyList<string> LastArguments { get; private set; } = [];

    public Task<CommandResult> RunZvecAsync(
        string command,
        IEnumerable<string>? arguments = null,
        IReadOnlyDictionary<string, string?>? environment = null,
        CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        LastCommand = command;
        LastArguments = arguments?.ToList() ?? [];
        return Task.FromResult(Result);
    }
}

file sealed class FakeSecureSecretStore : ISecureSecretStore
{
    public string? Value { get; private set; }

    public int DeleteCalls { get; private set; }

    public bool Exists() => Value is not null;

    public string? Read() => Value;

    public void Write(string secret)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(secret);
        Value = secret;
    }

    public void Delete()
    {
        DeleteCalls++;
        Value = null;
    }
}

file sealed class FakeRuntimeBootstrapper(string pythonExecutable) : IRuntimeBootstrapper
{
    public int Calls { get; private set; }

    public Task<RuntimeBootstrapResult> EnsureReadyAsync(
        string? bootstrapPython = null,
        CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        Calls++;
        return Task.FromResult(new RuntimeBootstrapResult
        {
            SchemaVersion = 1,
            Success = true,
            Status = "ready",
            Code = "ready",
            Message = "runtime ready",
            PythonExecutable = pythonExecutable,
            PythonVersion = "3.14.0",
            PythonArchitecture = "AMD64",
            Dependencies =
            [
                new RuntimeDependencyStatus
                {
                    Name = "zvec",
                    Version = "0.5.1",
                    Imported = true,
                },
                new RuntimeDependencyStatus
                {
                    Name = "Pillow",
                    Version = "11.3.0",
                    Imported = true,
                },
                new RuntimeDependencyStatus
                {
                    Name = "numpy",
                    Version = "2.3.2",
                    Imported = true,
                },
            ],
        });
    }
}

file sealed class FakeBackendProcessFactory : IBackendProcessFactory
{
    public FakeBackendProcess Process { get; } = new();

    public IBackendProcess Create() => Process;
}

file sealed class FakeBackendProcess : IBackendProcess
{
    private readonly DateTimeOffset _startTimeUtc = DateTimeOffset.UtcNow;

    public TaskCompletionSource Started { get; } = new(
        TaskCreationOptions.RunContinuationsAsynchronously
    );

    public ProcessStartInfo? StartInfo { get; private set; }

    public bool HasExited { get; private set; }

    public int? ExitCode => HasExited ? 0 : null;

    public int? ProcessId => 4242;

    public DateTimeOffset? StartTimeUtc => _startTimeUtc;

    public string RecentOutput => string.Empty;

    public int StopCalls { get; private set; }

    public string RuntimeConfigJson { get; private set; } = string.Empty;

    public Task StartAsync(
        ProcessStartInfo startInfo,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        StartInfo = startInfo;
        var arguments = startInfo.ArgumentList.ToArray();
        var configArgument = Array.IndexOf(arguments, "--libraries-config");
        if (configArgument >= 0 && configArgument + 1 < arguments.Length)
        {
            RuntimeConfigJson = File.ReadAllText(arguments[configArgument + 1]);
        }
        Started.TrySetResult();
        return Task.CompletedTask;
    }

    public Task StopAsync(TimeSpan timeout)
    {
        if (timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(timeout));
        }
        StopCalls++;
        HasExited = true;
        return Task.CompletedTask;
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
