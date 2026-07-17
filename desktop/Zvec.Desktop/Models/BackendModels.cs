using System.Text.Json;
using System.Text.Json.Serialization;
using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace Zvec.Desktop.Models;

public sealed class BackendHostOptions
{
    public bool AutoPrepareRuntime { get; init; } = true;

    public string? PythonExecutable { get; init; }

    public string? BackendEntryPointPath { get; init; }

    public string? ApplicationDirectory { get; init; }

    public string? QueryStagingDirectory { get; init; }

    public TimeSpan StartupTimeout { get; init; } = TimeSpan.FromSeconds(90);

    public TimeSpan HealthPollInterval { get; init; } = TimeSpan.FromMilliseconds(400);

    public TimeSpan HealthRequestTimeout { get; init; } = TimeSpan.FromSeconds(3);

    public TimeSpan ShutdownTimeout { get; init; } = TimeSpan.FromSeconds(3);

    public int RequiredProtocolVersion { get; init; } = 2;
}

public sealed class BackendHealthResponse
{
    [JsonPropertyName("instance_id")]
    public string InstanceId { get; init; } = string.Empty;

    [JsonPropertyName("config_fingerprint")]
    public string ConfigFingerprint { get; init; } = string.Empty;

    [JsonPropertyName("status")]
    public string Status { get; init; } = string.Empty;

    [JsonPropertyName("service_ready")]
    public bool ServiceReady { get; init; }

    [JsonPropertyName("worker_alive")]
    public bool WorkerAlive { get; init; }

    [JsonPropertyName("queue_depth")]
    public int QueueDepth { get; init; }

    [JsonPropertyName("running_jobs")]
    public int RunningJobs { get; init; }

    [JsonPropertyName("error")]
    public BackendJobError? Error { get; init; }

    [JsonPropertyName("protocol_version")]
    public int ProtocolVersion { get; init; }

    [JsonPropertyName("credentials_configured")]
    public bool CredentialsConfigured { get; init; }

    [JsonIgnore]
    public bool IsReady =>
        string.Equals(Status, "ok", StringComparison.OrdinalIgnoreCase) &&
        ServiceReady &&
        WorkerAlive;

    [JsonIgnore]
    public bool HasActiveWork => RunningJobs > 0 || QueueDepth > 0;
}

public sealed class BackendVersionResponse
{
    [JsonPropertyName("instance_id")]
    public string InstanceId { get; init; } = string.Empty;

    [JsonPropertyName("config_fingerprint")]
    public string ConfigFingerprint { get; init; } = string.Empty;

    [JsonPropertyName("protocol_version")]
    public int ProtocolVersion { get; init; }

    [JsonPropertyName("app")]
    public string App { get; init; } = string.Empty;

    [JsonPropertyName("version")]
    public string Version { get; init; } = string.Empty;

    [JsonPropertyName("capabilities")]
    public BackendCapabilities Capabilities { get; init; } = new();

    [JsonPropertyName("credentials_configured")]
    public bool CredentialsConfigured { get; init; }
}

public sealed class BackendCapabilities
{
    [JsonPropertyName("persistent_backend_session")]
    public bool PersistentBackendSession { get; init; }

    [JsonPropertyName("graceful_shutdown")]
    public bool GracefulShutdown { get; init; }

    [JsonPropertyName("multi_library")]
    public bool MultiLibrary { get; init; }

    [JsonPropertyName("federated_search")]
    public bool FederatedSearch { get; init; }

    [JsonPropertyName("session_credentials")]
    public bool SessionCredentials { get; init; }

    [JsonPropertyName("low_confidence_override")]
    public bool LowConfidenceOverride { get; init; }

    [JsonPropertyName("tag_only_search")]
    public bool TagOnlySearch { get; init; }

    [JsonPropertyName("hybrid_tag_vector_search")]
    public bool HybridTagVectorSearch { get; init; }

    [JsonPropertyName("metadata_embedding_search")]
    public bool MetadataEmbeddingSearch { get; init; }

    [JsonPropertyName("metadata_embedding_backfill")]
    public bool MetadataEmbeddingBackfill { get; init; }

    [JsonPropertyName("result_diversity")]
    public bool ResultDiversity { get; init; }

    [JsonPropertyName("concurrent_jobs")]
    public bool ConcurrentJobs { get; init; }

    [JsonPropertyName("job_list")]
    public bool JobList { get; init; }

    [JsonPropertyName("partial_jobs")]
    public bool PartialJobs { get; init; }

    [JsonPropertyName("job_pause_resume")]
    public bool JobPauseResume { get; init; }

    [JsonPropertyName("failure_paging")]
    public bool FailurePaging { get; init; }

    [JsonPropertyName("index_and_auto_tag")]
    public bool IndexAndAutoTag { get; init; }
}

public sealed class BackendCredentialRequest
{
    [JsonPropertyName("dashscope_api_key")]
    public string DashScopeApiKey { get; init; } = string.Empty;

    [JsonPropertyName("api_url")]
    public string? ApiUrl { get; init; }
}

public sealed class BackendCredentialResponse
{
    [JsonPropertyName("credentials_configured")]
    public bool CredentialsConfigured { get; init; }
}

public sealed class BackendJobRequest
{
    public BackendJobRequest()
    {
    }

    public BackendJobRequest(
        string command,
        IReadOnlyDictionary<string, object?>? parameters = null)
    {
        Command = command;
        Parameters = parameters is null
            ? new Dictionary<string, object?>()
            : new Dictionary<string, object?>(parameters, StringComparer.Ordinal);
    }

    [JsonPropertyName("command")]
    public string Command { get; init; } = string.Empty;

    [JsonPropertyName("params")]
    public Dictionary<string, object?> Parameters { get; init; } = [];
}

public sealed class BackendJobEnvelope
{
    [JsonPropertyName("job")]
    public BackendJob? Job { get; init; }
}

public sealed class BackendJobListResponse
{
    [JsonPropertyName("jobs")]
    public List<BackendJob> Jobs { get; init; } = [];

    [JsonPropertyName("count")]
    public int Count { get; init; }

    [JsonPropertyName("total_count")]
    public int TotalCount { get; init; }
}

public sealed class BackendJob
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("command")]
    public string Command { get; init; } = string.Empty;

    [JsonPropertyName("params")]
    public JsonElement? Parameters { get; init; }

    [JsonPropertyName("status")]
    public string Status { get; init; } = string.Empty;

    [JsonPropertyName("submitted_at")]
    public DateTimeOffset? SubmittedAt { get; init; }

    [JsonPropertyName("started_at")]
    public DateTimeOffset? StartedAt { get; init; }

    [JsonPropertyName("finished_at")]
    public DateTimeOffset? FinishedAt { get; init; }

    [JsonPropertyName("cancel_requested")]
    public bool CancelRequested { get; init; }

    [JsonPropertyName("progress")]
    public BackendJobProgress? Progress { get; init; }

    [JsonPropertyName("result")]
    public JsonElement? Result { get; init; }

    [JsonPropertyName("error")]
    public BackendJobError? Error { get; init; }

    [JsonPropertyName("failure_count")]
    public int FailureCount { get; init; }

    [JsonIgnore]
    public bool IsTerminal =>
        string.Equals(Status, "succeeded", StringComparison.OrdinalIgnoreCase) ||
        string.Equals(Status, "partial", StringComparison.OrdinalIgnoreCase) ||
        string.Equals(Status, "needs_attention", StringComparison.OrdinalIgnoreCase) ||
        string.Equals(Status, "failed", StringComparison.OrdinalIgnoreCase) ||
        string.Equals(Status, "cancelled", StringComparison.OrdinalIgnoreCase);

    [JsonIgnore]
    public bool IsPaused =>
        string.Equals(Status, "paused", StringComparison.OrdinalIgnoreCase);

    [JsonIgnore]
    public bool NeedsAttention =>
        string.Equals(Status, "needs_attention", StringComparison.OrdinalIgnoreCase);

    [JsonIgnore]
    public bool IsPartial =>
        string.Equals(Status, "partial", StringComparison.OrdinalIgnoreCase);

    [JsonIgnore]
    public bool IsSuccessful =>
        string.Equals(Status, "succeeded", StringComparison.OrdinalIgnoreCase);
}

public sealed class BackendJobProgress
{
    [JsonPropertyName("message")]
    public string Message { get; init; } = string.Empty;

    [JsonPropertyName("updated_at")]
    public DateTimeOffset? UpdatedAt { get; init; }

    [JsonPropertyName("current")]
    public int? Current { get; init; }

    [JsonPropertyName("total")]
    public int? Total { get; init; }

    [JsonPropertyName("failed")]
    public int? Failed { get; init; }

    [JsonPropertyName("library_id")]
    public string LibraryId { get; init; } = string.Empty;

    [JsonPropertyName("library_name")]
    public string LibraryName { get; init; } = string.Empty;
}

public sealed class BackendTaskItem : INotifyPropertyChanged
{
    private BackendJob _job = new();
    private string _requestedStatus = string.Empty;

    public string Id => _job.Id;

    public string Command => _job.Command;

    public string Title => _job.Command switch
    {
        "index" => "建立索引",
        "index_and_auto_tag" => "索引并智能标注",
        "sync" => "同步图库",
        "auto_tag" => "图片智能标注",
        "folder_tag_backfill" => "补全文件夹标签",
        "metadata_backfill" => "生成描述向量",
        "search" => "搜索",
        _ => string.IsNullOrWhiteSpace(_job.Command) ? "后台任务" : _job.Command,
    };

    public string StatusText => _job.Status switch
    {
        "queued" => "排队中",
        "running" => "运行中",
        "pausing" => "正在暂停",
        "paused" => "已暂停",
        "needs_attention" => "需要处理",
        "cancelling" => "正在取消",
        "succeeded" => "已完成",
        "partial" => "部分完成",
        "failed" => "失败",
        "cancelled" => "已取消",
        _ => string.IsNullOrWhiteSpace(_job.Status) ? "准备中" : _job.Status,
    };

    public string Message => string.IsNullOrWhiteSpace(_job.Progress?.Message)
        ? _requestedStatus
        : _job.Progress.Message;

    public string LibraryText
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(_job.Progress?.LibraryName))
            {
                return _job.Progress.LibraryName;
            }
            if (_job.Parameters is JsonElement { ValueKind: JsonValueKind.Object } parameters &&
                parameters.TryGetProperty("library_id", out var libraryId) &&
                libraryId.ValueKind == JsonValueKind.String)
            {
                return libraryId.GetString() ?? string.Empty;
            }
            return string.Empty;
        }
    }

    public string ProgressText
    {
        get
        {
            var current = _job.Progress?.Current;
            var total = _job.Progress?.Total;
            var progress = current.HasValue && total is > 0
                ? $"{current.Value}/{total.Value}"
                : string.Empty;
            var failures = Math.Max(_job.FailureCount, _job.Progress?.Failed ?? 0);
            var failed = failures > 0 ? $"失败 {failures}" : string.Empty;
            return string.Join(" · ", new[] { progress, failed }.Where(value => value.Length > 0));
        }
    }

    public double ProgressMaximum => Math.Max(1, _job.Progress?.Total ?? 1);

    public double ProgressValue => Math.Max(0, _job.Progress?.Current ?? 0);

    public bool IsIndeterminate => _job.Progress?.Total is not > 0 && !_job.IsTerminal;

    public bool IsActive => !_job.IsTerminal && !_job.NeedsAttention;

    public bool CanCancel =>
        IsActive &&
        !string.Equals(_job.Status, "cancelling", StringComparison.OrdinalIgnoreCase) &&
        !string.Equals(_job.Status, "pausing", StringComparison.OrdinalIgnoreCase) &&
        !string.IsNullOrWhiteSpace(Id);

    public bool HasFailures => Math.Max(_job.FailureCount, _job.Progress?.Failed ?? 0) > 0;

    public string FailureManifestPath => GetResultString("failure_manifest");

    public int QuarantinedCount => GetResultInt32("quarantined");

    public bool CanOpenFailureDirectory =>
        QuarantinedCount > 0 && !string.IsNullOrWhiteSpace(FailureManifestPath);

    public bool IsTerminal => _job.IsTerminal;

    public bool NeedsAttention => _job.NeedsAttention;

    public DateTimeOffset? SubmittedAt => _job.SubmittedAt;

    public event PropertyChangedEventHandler? PropertyChanged;

    /// <returns>
    /// <see langword="true"/> only when a value rendered by the task center changed.
    /// Polling may return a new JSON object with identical progress; suppressing that
    /// redundant notification avoids rebuilding every visible row several times a second.
    /// </returns>
    public bool Update(BackendJob job, string? requestedStatus = null)
    {
        ArgumentNullException.ThrowIfNull(job);
        var previousState = BackendJobPresentationState.From(_job, _requestedStatus);
        _job = job;
        if (!string.IsNullOrWhiteSpace(requestedStatus))
        {
            _requestedStatus = requestedStatus;
        }
        var currentState = BackendJobPresentationState.From(_job, _requestedStatus);
        if (currentState == previousState)
        {
            return false;
        }
        OnPropertyChanged(string.Empty);
        return true;
    }

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));

    private string GetResultString(string propertyName)
    {
        if (_job.Result is JsonElement { ValueKind: JsonValueKind.Object } result &&
            result.TryGetProperty(propertyName, out var value) &&
            value.ValueKind == JsonValueKind.String)
        {
            return value.GetString() ?? string.Empty;
        }
        return string.Empty;
    }

    private int GetResultInt32(string propertyName)
    {
        if (_job.Result is JsonElement { ValueKind: JsonValueKind.Object } result &&
            result.TryGetProperty(propertyName, out var value) &&
            value.ValueKind == JsonValueKind.Number &&
            value.TryGetInt32(out var number))
        {
            return Math.Max(0, number);
        }
        return 0;
    }
}

/// <summary>
/// Immutable projection of the backend fields consumed by task-center bindings.
/// Keeping this independent from WPF also makes polling coalescing directly testable.
/// </summary>
internal readonly record struct BackendJobPresentationState(
    string Id,
    string Command,
    string Status,
    string RequestedStatus,
    string Message,
    string LibraryId,
    string LibraryName,
    int? Current,
    int? Total,
    int? ProgressFailed,
    int FailureCount,
    string FailureManifestPath,
    int QuarantinedCount,
    DateTimeOffset? SubmittedAt)
{
    public static BackendJobPresentationState From(
        BackendJob job,
        string requestedStatus = "")
    {
        ArgumentNullException.ThrowIfNull(job);
        return new BackendJobPresentationState(
            job.Id,
            job.Command,
            job.Status,
            requestedStatus,
            job.Progress?.Message ?? string.Empty,
            ReadString(job.Parameters, "library_id"),
            job.Progress?.LibraryName ?? string.Empty,
            job.Progress?.Current,
            job.Progress?.Total,
            job.Progress?.Failed,
            job.FailureCount,
            ReadString(job.Result, "failure_manifest"),
            ReadInt32(job.Result, "quarantined"),
            job.SubmittedAt
        );
    }

    private static string ReadString(JsonElement? source, string propertyName)
    {
        if (source is JsonElement { ValueKind: JsonValueKind.Object } value &&
            value.TryGetProperty(propertyName, out var property) &&
            property.ValueKind == JsonValueKind.String)
        {
            return property.GetString() ?? string.Empty;
        }
        return string.Empty;
    }

    private static int ReadInt32(JsonElement? source, string propertyName)
    {
        if (source is JsonElement { ValueKind: JsonValueKind.Object } value &&
            value.TryGetProperty(propertyName, out var property) &&
            property.TryGetInt32(out var result))
        {
            return result;
        }
        return 0;
    }
}

public static class FailureDirectoryResolver
{
    public static bool TryResolve(
        string resultsDirectory,
        string failureManifestPath,
        int quarantinedCount,
        out string failureDirectory)
    {
        failureDirectory = string.Empty;
        if (quarantinedCount <= 0 ||
            string.IsNullOrWhiteSpace(resultsDirectory) ||
            string.IsNullOrWhiteSpace(failureManifestPath))
        {
            return false;
        }
        try
        {
            var root = Path.GetFullPath(Path.Combine(resultsDirectory, "failed-images"));
            var manifest = Path.GetFullPath(failureManifestPath);
            var rootPrefix = Path.TrimEndingDirectorySeparator(root) + Path.DirectorySeparatorChar;
            var comparison = OperatingSystem.IsWindows()
                ? StringComparison.OrdinalIgnoreCase
                : StringComparison.Ordinal;
            if (!manifest.StartsWith(rootPrefix, comparison) ||
                !File.Exists(manifest) ||
                !Directory.Exists(root))
            {
                return false;
            }
            failureDirectory = root;
            return true;
        }
        catch (Exception exception) when (
            exception is ArgumentException or IOException or NotSupportedException or
                UnauthorizedAccessException
        )
        {
            return false;
        }
    }
}

public sealed class BackendJobError
{
    [JsonPropertyName("code")]
    public string Code { get; init; } = string.Empty;

    [JsonPropertyName("message")]
    public string Message { get; init; } = string.Empty;

    [JsonPropertyName("details")]
    public JsonElement? Details { get; init; }
}

public sealed record BackendStagedQueryFile(
    string OriginalPath,
    string HostPath,
    string BackendPath
);

public sealed class BackendApiException : Exception
{
    public BackendApiException(
        System.Net.HttpStatusCode statusCode,
        string responseBody,
        string message)
        : base(message)
    {
        StatusCode = statusCode;
        ResponseBody = responseBody;
    }

    public System.Net.HttpStatusCode StatusCode { get; }

    public string ResponseBody { get; }
}

public sealed class BackendProtocolException : Exception
{
    public BackendProtocolException(string message)
        : base(message)
    {
    }
}
