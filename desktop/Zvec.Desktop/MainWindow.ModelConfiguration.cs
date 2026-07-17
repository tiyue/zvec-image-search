using System.Diagnostics;
using System.Net.Http;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Zvec.Desktop.Models;
using Zvec.Desktop.Services;

namespace Zvec.Desktop;

public partial class MainWindow
{
    private readonly ModelConfigurationService _modelConfigurationService = new();
    private ModelConfigurationDocument _activeModelConfiguration =
        ModelConfigurationService.CreateDefault();
    private bool _modelConfigurationLoaded;
    private bool _modelConfigurationOperationInProgress;
    private bool _modelConfigurationRestartPending;
    private bool _modelConfigurationRestartScheduled;

    private async void ModelConfigurationTab_Loaded(object sender, RoutedEventArgs e)
    {
        ModelConfigurationPathTextBlock.Text = _modelConfigurationService.ConfigurationPath;
        if (_modelConfigurationLoaded)
        {
            return;
        }
        await ReloadModelConfigurationAsync(announceInLog: false);
    }

    private async void ReloadModelConfiguration_Click(object sender, RoutedEventArgs e) =>
        await ReloadModelConfigurationAsync(announceInLog: true);

    private async Task ReloadModelConfigurationAsync(bool announceInLog)
    {
        if (_modelConfigurationOperationInProgress)
        {
            return;
        }
        SetModelConfigurationOperationState(isBusy: true);
        try
        {
            var hadLoadedConfiguration = _modelConfigurationLoaded;
            var previousHash = BackendConfigurationFingerprint.ComputeModelConfigurationHash(
                _activeModelConfiguration
            );
            var result = await _modelConfigurationService.LoadAsync(createIfMissing: true);
            string? application = null;
            if (result.IsValid)
            {
                _activeModelConfiguration = result.Configuration;
                PopulateModelRoleChoices(result.Configuration);
                var currentHash = BackendConfigurationFingerprint.ComputeModelConfigurationHash(
                    result.Configuration
                );
                if (hadLoadedConfiguration && !string.Equals(
                    previousHash,
                    currentHash,
                    StringComparison.Ordinal
                ))
                {
                    application = await ApplySavedModelConfigurationToBackendAsync();
                }
            }
            else if (!_modelConfigurationLoaded)
            {
                // On the first invalid load, show the safe built-in fallback. Later
                // invalid reloads intentionally leave the last valid UI selection intact.
                _activeModelConfiguration = result.Configuration;
                PopulateModelRoleChoices(result.Configuration);
            }

            _modelConfigurationLoaded = true;
            if (result.IsValid)
            {
                var message = result.WasCreated
                    ? "已创建默认模型配置。API Key 仍安全保存在 Windows 凭据管理器中。"
                    : application is null
                        ? "模型配置有效，内容没有变化。"
                        : $"模型配置已重新载入。{application}";
                SetModelConfigurationStatus(message, isError: false);
                if (announceInLog)
                {
                    AppendLog(message, isError: false);
                }
            }
            else
            {
                var message =
                    $"模型配置无效：{result.Error} 已保留并继续使用上次有效配置。";
                SetModelConfigurationStatus(message, isError: true);
                if (announceInLog)
                {
                    AppendLog(message, isError: true);
                }
            }
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or InvalidDataException)
        {
            var message = $"读取模型配置失败：{exception.Message}";
            SetModelConfigurationStatus(message, isError: true);
            AppendLog(message, isError: true);
        }
        finally
        {
            SetModelConfigurationOperationState(isBusy: false);
        }
    }

    private async void SaveModelConfiguration_Click(object sender, RoutedEventArgs e)
    {
        if (_modelConfigurationOperationInProgress)
        {
            return;
        }
        if (EmbeddingModelComboBox.SelectedItem is not ModelChoiceItem embedding ||
            AutoTagPrimaryModelComboBox.SelectedItem is not ModelChoiceItem primary ||
            AutoTagEscalationModelComboBox.SelectedItem is not ModelChoiceItem escalation)
        {
            SetModelConfigurationStatus("请为三个模型角色分别选择一个可用模型。", isError: true);
            return;
        }
        if (!string.Equals(
            embedding.Id,
            _activeModelConfiguration.Roles?.Embedding,
            StringComparison.Ordinal
        ))
        {
            var answer = MessageBox.Show(
                this,
                "更换向量模型只会用于新建或重建索引。已有 Collection 会因模型元数据不一致而拒绝打开，防止混用不同向量。是否继续保存？",
                "确认更换向量模型",
                MessageBoxButton.YesNo,
                MessageBoxImage.Warning
            );
            if (answer != MessageBoxResult.Yes)
            {
                return;
            }
        }

        SetModelConfigurationOperationState(isBusy: true);
        try
        {
            var candidate = ModelConfigurationService.WithRoleAssignments(
                _activeModelConfiguration,
                embedding.Id,
                primary.Id,
                escalation.Id
            );
            _activeModelConfiguration = await _modelConfigurationService.SaveAsync(candidate);
            PopulateModelRoleChoices(_activeModelConfiguration);
            var application = await ApplySavedModelConfigurationToBackendAsync();
            var message =
                $"模型配置已原子保存，API Key 未写入 JSON。{application}";
            SetModelConfigurationStatus(message, isError: false);
            AppendLog(message, isError: false);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or InvalidDataException)
        {
            var message = $"保存模型配置失败：{exception.Message}";
            SetModelConfigurationStatus(message, isError: true);
            AppendLog(message, isError: true);
        }
        finally
        {
            SetModelConfigurationOperationState(isBusy: false);
        }
    }

    private async Task<string> ApplySavedModelConfigurationToBackendAsync()
    {
        var backend = _backendHost;
        if (backend?.IsRunning != true)
        {
            _modelConfigurationRestartPending = false;
            return "常驻后端下次启动时会使用新配置。";
        }
        try
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            var health = await backend.ApiClient.GetHealthAsync(timeout.Token);
            _modelConfigurationRestartPending = true;
            if (health.HasActiveWork)
            {
                return "当前任务不会被中断；任务结束后将自动安全重启并应用新配置。";
            }
            var restarted = await RestartBackendForModelConfigurationAsync();
            return restarted
                ? "空闲后端已安全重启，后续任务立即使用新配置。"
                : "后端状态发生变化，已保留后台运行；空闲后会自动重试应用。";
        }
        catch (Exception exception) when (
            exception is HttpRequestException or TaskCanceledException or BackendApiException or
                BackendProtocolException)
        {
            _modelConfigurationRestartPending = true;
            AppendLog($"暂时无法确认后端是否空闲：{exception.Message}", isError: false);
            return "暂时无法确认后端是否空闲，未终止任何任务；下次后端启动时应用。";
        }
    }

    private void ScheduleModelConfigurationRestartIfIdle(int activeCount)
    {
        if (!_modelConfigurationRestartPending || activeCount != 0 ||
            _modelConfigurationRestartScheduled || _isClosing ||
            _backendHost?.IsDrainOnly == true)
        {
            return;
        }
        _ = Dispatcher.BeginInvoke(new Action(async () =>
        {
            await RestartBackendForModelConfigurationAsync();
        }));
    }

    private async Task<bool> RestartBackendForModelConfigurationAsync()
    {
        if (_modelConfigurationRestartScheduled)
        {
            return false;
        }
        _modelConfigurationRestartScheduled = true;
        try
        {
            var stopped = false;
            await _backendLifecycle.RunExclusiveAsync(async () =>
            {
                var backend = _backendHost;
                if (backend?.IsRunning != true)
                {
                    stopped = true;
                    return;
                }
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                BackendHealthResponse health;
                try
                {
                    health = await backend.ApiClient.GetHealthAsync(timeout.Token);
                }
                catch (Exception exception) when (
                    exception is HttpRequestException or TaskCanceledException or BackendApiException or
                        BackendProtocolException)
                {
                    AppendLog(
                        $"无法安全确认模型配置重启条件：{exception.Message}",
                        isError: false
                    );
                    return;
                }
                if (health.HasActiveWork)
                {
                    return;
                }
                try
                {
                    await backend.StopAsync();
                }
                catch (InvalidOperationException)
                {
                    // A job won the race after the health probe. Keep the same host and
                    // monitor; the next terminal snapshot will schedule another attempt.
                    return;
                }
                await StopBackendJobMonitorAsync();
                if (ReferenceEquals(_backendHost, backend))
                {
                    _backendHost = null;
                }
                await backend.DisposeAsync();
                UpdateLowConfidenceCapability(false);
                stopped = true;
            });

            if (!stopped)
            {
                return false;
            }
            if (_isClosing)
            {
                return true;
            }
            SetBackendStatus("正在应用新的模型配置…", BackendStatusKind.Starting);
            var started = await TryStartBackendAsync(showFailure: true);
            if (started)
            {
                _modelConfigurationRestartPending = false;
            }
            return started;
        }
        catch (Exception exception)
        {
            AppendLog($"应用新的模型配置失败：{exception.Message}", isError: true);
            return false;
        }
        finally
        {
            _modelConfigurationRestartScheduled = false;
        }
    }

    private async void OpenModelConfiguration_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            if (!File.Exists(_modelConfigurationService.ConfigurationPath))
            {
                await _modelConfigurationService.LoadAsync(createIfMissing: true);
            }
            Process.Start(new ProcessStartInfo
            {
                FileName = _modelConfigurationService.ConfigurationPath,
                UseShellExecute = true,
            });
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            var message = $"打开模型配置失败：{exception.Message}";
            SetModelConfigurationStatus(message, isError: true);
            AppendLog(message, isError: true);
        }
    }

    private void ModelRoleSelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        UpdateModelChoiceDetails(
            EmbeddingModelComboBox,
            EmbeddingModelDetailsTextBlock
        );
        UpdateModelChoiceDetails(
            AutoTagPrimaryModelComboBox,
            AutoTagPrimaryModelDetailsTextBlock
        );
        UpdateModelChoiceDetails(
            AutoTagEscalationModelComboBox,
            AutoTagEscalationModelDetailsTextBlock
        );
    }

    private void PopulateModelRoleChoices(ModelConfigurationDocument configuration)
    {
        var models = configuration.Models ?? [];
        SetRoleChoices(
            EmbeddingModelComboBox,
            models,
            ModelRoles.Embedding,
            configuration.Roles?.Embedding
        );
        SetRoleChoices(
            AutoTagPrimaryModelComboBox,
            models,
            ModelRoles.AutoTagPrimary,
            configuration.Roles?.AutoTagPrimary
        );
        SetRoleChoices(
            AutoTagEscalationModelComboBox,
            models,
            ModelRoles.AutoTagEscalation,
            configuration.Roles?.AutoTagEscalation
        );
        UpdateAllModelChoiceDetails();
        RefreshAutoTagTaskModelChoices();
    }

    private void RefreshAutoTagTaskModelChoices()
    {
        var previouslySelected = (SmartTagModelComboBox.SelectedItem as ModelChoiceItem)?.Id;
        var models = _activeModelConfiguration.Models ?? [];
        var choices = models
            .Where(model =>
                model.Enabled == true &&
                (model.Roles?.Contains(
                    ModelRoles.AutoTagPrimary,
                    StringComparer.Ordinal
                ) == true || model.Roles?.Contains(
                    ModelRoles.AutoTagEscalation,
                    StringComparer.Ordinal
                ) == true)
            )
            .Select(model => new ModelChoiceItem(
                model.Id!,
                model.DisplayName!,
                ModelConfigurationService.DescribeCapability(model),
                model.Protocol!
            ))
            .OrderBy(choice => choice.DisplayName, StringComparer.CurrentCultureIgnoreCase)
            .ThenBy(choice => choice.Id, StringComparer.Ordinal)
            .ToList();
        SmartTagModelComboBox.ItemsSource = choices;
        IndexAutoTagModelComboBox.ItemsSource = choices;
        var preferredId = choices.Any(choice => string.Equals(
            choice.Id,
            previouslySelected,
            StringComparison.Ordinal
        ))
            ? previouslySelected
            : _activeModelConfiguration.Roles?.AutoTagPrimary;
        SmartTagModelComboBox.SelectedItem = choices.FirstOrDefault(choice => string.Equals(
            choice.Id,
            preferredId,
            StringComparison.Ordinal
        )) ?? choices.FirstOrDefault();
        UpdateAutoTagTaskModelPolicy();
    }

    private void SmartTagTaskModelSelectionChanged(
        object sender,
        SelectionChangedEventArgs e) => UpdateAutoTagTaskModelPolicy();

    private void UpdateAutoTagTaskModelPolicy()
    {
        if (SmartTagModelComboBox.SelectedItem is not ModelChoiceItem selected)
        {
            SmartTagModelPolicyTextBlock.Text = "当前没有可用的常规标注模型。";
            IndexAutoTagModelPolicyTextBlock.Text = SmartTagModelPolicyTextBlock.Text;
            return;
        }
        var primaryId = _activeModelConfiguration.Roles?.AutoTagPrimary;
        var escalationId = _activeModelConfiguration.Roles?.AutoTagEscalation;
        var escalationName = _activeModelConfiguration.Models?
            .FirstOrDefault(model => string.Equals(
                model.Id,
                escalationId,
                StringComparison.Ordinal
            ))?.DisplayName ?? escalationId ?? "未配置";
        var policy = string.Equals(selected.Id, primaryId, StringComparison.Ordinal)
            ? $"主模型：{selected.DisplayName}；冲突或低置信时升级：{escalationName}。"
            : $"手动全程使用：{selected.DisplayName}；本次不先调用默认主模型。";
        SmartTagModelPolicyTextBlock.Text = policy;
        IndexAutoTagModelPolicyTextBlock.Text = policy;
    }

    private static void SetRoleChoices(
        ComboBox comboBox,
        IEnumerable<ModelDefinition> models,
        string role,
        string? selectedModelId)
    {
        var choices = models
            .Where(model =>
                model.Enabled == true &&
                model.Roles?.Contains(role, StringComparer.Ordinal) == true
            )
            .Select(model => new ModelChoiceItem(
                model.Id!,
                model.DisplayName!,
                ModelConfigurationService.DescribeCapability(model),
                model.Protocol!
            ))
            .OrderBy(choice => choice.DisplayName, StringComparer.CurrentCultureIgnoreCase)
            .ThenBy(choice => choice.Id, StringComparer.Ordinal)
            .ToList();
        comboBox.ItemsSource = choices;
        comboBox.SelectedItem = choices.FirstOrDefault(choice => string.Equals(
            choice.Id,
            selectedModelId,
            StringComparison.Ordinal
        ));
    }

    private static void UpdateModelChoiceDetails(ComboBox comboBox, TextBlock target)
    {
        target.Text = comboBox.SelectedItem is ModelChoiceItem choice
            ? $"名称：{choice.DisplayName}\nID：{choice.Id}\n能力：{choice.Capability}"
            : "当前角色没有可用模型。请打开 JSON 添加兼容模型后重新载入。";
    }

    private void UpdateAllModelChoiceDetails()
    {
        UpdateModelChoiceDetails(
            EmbeddingModelComboBox,
            EmbeddingModelDetailsTextBlock
        );
        UpdateModelChoiceDetails(
            AutoTagPrimaryModelComboBox,
            AutoTagPrimaryModelDetailsTextBlock
        );
        UpdateModelChoiceDetails(
            AutoTagEscalationModelComboBox,
            AutoTagEscalationModelDetailsTextBlock
        );
    }

    private void SetModelConfigurationOperationState(bool isBusy)
    {
        _modelConfigurationOperationInProgress = isBusy;
        ReloadModelConfigurationButton.IsEnabled = !isBusy;
        OpenModelConfigurationButton.IsEnabled = !isBusy;
        SaveModelConfigurationButton.IsEnabled = !isBusy;
        EmbeddingModelComboBox.IsEnabled = !isBusy;
        AutoTagPrimaryModelComboBox.IsEnabled = !isBusy;
        AutoTagEscalationModelComboBox.IsEnabled = !isBusy;
    }

    private void SetModelConfigurationStatus(string message, bool isError)
    {
        ModelConfigurationStatusTextBlock.Text = message;
        ModelConfigurationStatusTextBlock.Foreground = (Brush)FindResource(
            isError ? "DangerBrush" : "SuccessBrush"
        );
    }
}
