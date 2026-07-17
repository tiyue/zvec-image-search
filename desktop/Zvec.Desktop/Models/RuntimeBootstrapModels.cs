using System.Text.Json.Serialization;

namespace Zvec.Desktop.Models;

public sealed record RuntimeDependencyStatus
{
    [JsonPropertyName("name")]
    public string Name { get; init; } = string.Empty;

    [JsonPropertyName("version")]
    public string Version { get; init; } = string.Empty;

    [JsonPropertyName("imported")]
    public bool Imported { get; init; }
}

public sealed record RuntimeBootstrapResult
{
    [JsonPropertyName("schema_version")]
    public int SchemaVersion { get; init; }

    [JsonPropertyName("success")]
    public bool Success { get; init; }

    [JsonPropertyName("status")]
    public string Status { get; init; } = string.Empty;

    [JsonPropertyName("changed")]
    public bool Changed { get; init; }

    [JsonPropertyName("code")]
    public string Code { get; init; } = string.Empty;

    [JsonPropertyName("message")]
    public string Message { get; init; } = string.Empty;

    [JsonPropertyName("recommended_action")]
    public string? RecommendedAction { get; init; }

    [JsonPropertyName("python_executable")]
    public string? PythonExecutable { get; init; }

    [JsonPropertyName("python_version")]
    public string? PythonVersion { get; init; }

    [JsonPropertyName("python_architecture")]
    public string? PythonArchitecture { get; init; }

    [JsonPropertyName("runtime_directory")]
    public string? RuntimeDirectory { get; init; }

    [JsonPropertyName("dependencies")]
    public IReadOnlyList<RuntimeDependencyStatus> Dependencies { get; init; } =
        Array.Empty<RuntimeDependencyStatus>();

    [JsonIgnore]
    public string DiagnosticOutput { get; init; } = string.Empty;

    [JsonIgnore]
    public bool IsSuccess => Success &&
        (string.Equals(Status, "ready", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(Status, "repaired", StringComparison.OrdinalIgnoreCase));
}

public sealed class RuntimeBootstrapException : InvalidOperationException
{
    public RuntimeBootstrapException(RuntimeBootstrapResult result)
        : base(FormatMessage(result))
    {
        Result = result ?? throw new ArgumentNullException(nameof(result));
    }

    public RuntimeBootstrapResult Result { get; }

    private static string FormatMessage(RuntimeBootstrapResult result)
    {
        ArgumentNullException.ThrowIfNull(result);
        if (string.IsNullOrWhiteSpace(result.RecommendedAction))
        {
            return result.Message;
        }
        return $"{result.Message}{Environment.NewLine}建议：{result.RecommendedAction}";
    }
}
