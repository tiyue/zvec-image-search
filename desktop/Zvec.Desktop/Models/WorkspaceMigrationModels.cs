using System.Text.Json;
using System.Text.Json.Serialization;

namespace Zvec.Desktop.Models;

public sealed record WorkspaceBackupRequest(
    string? LibraryId = null,
    string? Destination = null,
    bool FullBackup = false);

public sealed record WorkspaceMigrationRequest(
    string? LibraryId = null,
    string? ExportDestination = null,
    string? BackupDirectory = null,
    bool FullBackup = false);

public sealed class WorkspaceBackupPlan
{
    [JsonPropertyName("status")]
    public string Status { get; init; } = string.Empty;

    [JsonPropertyName("backup_mode")]
    public string BackupMode { get; init; } = string.Empty;

    [JsonPropertyName("destination")]
    public string Destination { get; init; } = string.Empty;

    [JsonPropertyName("estimated_payload_bytes")]
    public long EstimatedPayloadBytes { get; init; }

    [JsonPropertyName("required_free_bytes")]
    public long RequiredFreeBytes { get; init; }

    [JsonPropertyName("available_free_bytes")]
    public long? AvailableFreeBytes { get; init; }

    [JsonPropertyName("destination_writable")]
    public bool DestinationWritable { get; init; }

    [JsonPropertyName("libraries")]
    public List<WorkspaceBackupLibraryPlan> Libraries { get; init; } = [];

    [JsonPropertyName("blockers")]
    public List<string> Blockers { get; init; } = [];

    [JsonPropertyName("warnings")]
    public List<string> Warnings { get; init; } = [];

    [JsonPropertyName("api_requests")]
    public int ApiRequests { get; init; }

    [JsonIgnore]
    public bool CanProceed => string.Equals(Status, "ready", StringComparison.Ordinal);
}

public sealed class WorkspaceBackupLibraryPlan
{
    [JsonPropertyName("library_id")]
    public string LibraryId { get; init; } = string.Empty;

    [JsonPropertyName("name")]
    public string Name { get; init; } = string.Empty;

    [JsonPropertyName("workspace")]
    public string Workspace { get; init; } = string.Empty;

    [JsonPropertyName("has_index")]
    public bool HasIndex { get; init; }

    [JsonPropertyName("sqlite_entries")]
    public long? SqliteEntries { get; init; }

    [JsonPropertyName("included_bytes")]
    public long IncludedBytes { get; init; }

    [JsonPropertyName("collection_bytes")]
    public long? CollectionBytes { get; init; }

    [JsonPropertyName("collection_files")]
    public long? CollectionFiles { get; init; }

    [JsonPropertyName("collection_included")]
    public bool CollectionIncluded { get; init; }

    [JsonPropertyName("blockers")]
    public List<string> Blockers { get; init; } = [];

    [JsonPropertyName("warnings")]
    public List<string> Warnings { get; init; } = [];
}

public sealed class WorkspaceBackupReport
{
    [JsonPropertyName("status")]
    public string Status { get; init; } = string.Empty;

    [JsonPropertyName("backup_mode")]
    public string? BackupMode { get; init; }

    [JsonPropertyName("destination")]
    public string? Destination { get; init; }

    [JsonPropertyName("manifest")]
    public string? Manifest { get; init; }

    [JsonPropertyName("file_count")]
    public int? FileCount { get; init; }

    [JsonPropertyName("plan")]
    public WorkspaceBackupPlan? Plan { get; init; }

    [JsonPropertyName("api_requests")]
    public int ApiRequests { get; init; }
}

public sealed class WorkspaceMigrationReport
{
    [JsonPropertyName("status")]
    public string Status { get; init; } = string.Empty;

    [JsonPropertyName("config_schema")]
    public int? ConfigSchema { get; init; }

    // Schema-v3 verification returns the workspace backup here, while legacy
    // migration uses this field for the small original-config backup path.
    [JsonPropertyName("backup")]
    public JsonElement? Backup { get; init; }

    [JsonPropertyName("workspace_backup")]
    public WorkspaceBackupReport? LegacyWorkspaceBackup { get; init; }

    [JsonPropertyName("exports")]
    public List<WorkspaceExportReport> Exports { get; init; } = [];

    [JsonPropertyName("verification")]
    public List<WorkspaceVerificationReport> Verification { get; init; } = [];

    [JsonPropertyName("api_requests")]
    public int ApiRequests { get; init; }

    [JsonIgnore]
    public bool RequiresNamedVolumeExport => Exports.Count > 0;

    [JsonIgnore]
    public WorkspaceBackupReport? WorkspaceBackup
    {
        get
        {
            if (LegacyWorkspaceBackup is not null)
            {
                return LegacyWorkspaceBackup;
            }
            if (Backup is not { ValueKind: JsonValueKind.Object } backup)
            {
                return null;
            }
            return backup.Deserialize<WorkspaceBackupReport>();
        }
    }

    [JsonIgnore]
    public WorkspaceBackupReport? EffectiveWorkspaceBackup => WorkspaceBackup;

    [JsonIgnore]
    public string? ConfigBackupPath =>
        Backup is { ValueKind: JsonValueKind.String } backup
            ? backup.GetString()
            : null;
}

public sealed class WorkspaceExportReport
{
    [JsonPropertyName("status")]
    public string Status { get; init; } = string.Empty;

    [JsonPropertyName("volume")]
    public string Volume { get; init; } = string.Empty;

    [JsonPropertyName("destination")]
    public string Destination { get; init; } = string.Empty;
}

public sealed class WorkspaceVerificationReport
{
    [JsonPropertyName("library_id")]
    public string LibraryId { get; init; } = string.Empty;

    [JsonPropertyName("status")]
    public string Status { get; init; } = string.Empty;

    [JsonPropertyName("collection_documents")]
    public long? CollectionDocuments { get; init; }

    [JsonPropertyName("sqlite_entries")]
    public long? SqliteEntries { get; init; }

    [JsonPropertyName("search_probe")]
    public string? SearchProbe { get; init; }

    [JsonPropertyName("api_requests")]
    public int ApiRequests { get; init; }
}

public sealed record WorkspaceCommandResponse<T>(T Report, CommandResult Command)
{
    public bool IsSuccess => Command.IsSuccess;
}
