using System.Text.Json.Serialization;

namespace Zvec.Desktop.Models;

public static class BackendInstanceStates
{
    public const string Starting = "starting";
    public const string Ready = "ready";

    public static bool IsValid(string? state) =>
        string.Equals(state, Starting, StringComparison.Ordinal) ||
        string.Equals(state, Ready, StringComparison.Ordinal);
}

public enum BackendConnectionMode
{
    None,
    Spawned,
    Attached,
}

/// <summary>
/// Public connection metadata for one persistent local backend. Authentication
/// material is deliberately stored outside this document.
/// </summary>
public sealed class BackendInstanceDescriptor
{
    [JsonPropertyName("instance_id")]
    public string InstanceId { get; init; } = string.Empty;

    [JsonPropertyName("host")]
    public string Host { get; init; } = string.Empty;

    [JsonPropertyName("port")]
    public int Port { get; init; }

    [JsonPropertyName("wrapper_pid")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? WrapperPid { get; init; }

    [JsonPropertyName("server_pid")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? ServerPid { get; init; }

    [JsonPropertyName("process_start_utc")]
    public DateTimeOffset ProcessStartUtc { get; init; }

    [JsonPropertyName("config_fingerprint")]
    public string ConfigFingerprint { get; init; } = string.Empty;

    [JsonPropertyName("query_root")]
    public string QueryRoot { get; init; } = string.Empty;

    [JsonPropertyName("runtime_config_path")]
    public string RuntimeConfigPath { get; init; } = string.Empty;

    [JsonPropertyName("state")]
    public string State { get; init; } = BackendInstanceStates.Starting;

    [JsonPropertyName("generated_utc")]
    public DateTimeOffset GeneratedUtc { get; init; }

    [JsonIgnore]
    public bool IsReady => string.Equals(
        State,
        BackendInstanceStates.Ready,
        StringComparison.Ordinal
    );
}
