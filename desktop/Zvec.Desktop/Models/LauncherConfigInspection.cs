namespace Zvec.Desktop.Models;

public sealed record LauncherConfigInspection
{
    public bool Exists { get; init; }

    public int SchemaVersion { get; init; }

    public bool RequiresMigration => SchemaVersion is 1 or 2;

    public bool HasNamedVolume { get; init; }

    public bool DockerRequired => HasNamedVolume;

    public string DefaultBackupRoot { get; init; } = string.Empty;

    public IReadOnlyList<LauncherNamedVolumeInspection> NamedVolumes { get; init; } = [];

    public IReadOnlyList<string> Blockers { get; init; } = [];

    public int ApiRequests { get; init; }

    public string? DefaultLibraryId { get; init; }

    public string? LibraryName { get; init; }

    public string? ImageRoot { get; init; }

    public string? WorkspaceType { get; init; }

    public string? WorkspaceSource { get; init; }

    public string? ResultsDirectory { get; init; }
}

public sealed record LauncherNamedVolumeInspection
{
    public string LibraryId { get; init; } = string.Empty;

    public string LibraryName { get; init; } = string.Empty;

    public string VolumeName { get; init; } = string.Empty;

    public string DefaultExportDirectory { get; init; } = string.Empty;
}
