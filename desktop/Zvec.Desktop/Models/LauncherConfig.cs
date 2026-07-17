using System.Text.Json.Serialization;

namespace Zvec.Desktop.Models;

public sealed class LauncherConfig
{
    public const int CurrentSchemaVersion = 3;

    [JsonPropertyName("schema_version")]
    public int SchemaVersion { get; set; }

    [JsonPropertyName("python_executable")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? PythonExecutable { get; set; }

    [JsonPropertyName("results_directory")]
    public string ResultsDirectory { get; set; } = string.Empty;

    [JsonPropertyName("default_library_id")]
    public string DefaultLibraryId { get; set; } = string.Empty;

    [JsonPropertyName("libraries")]
    public List<LauncherLibrary> Libraries { get; set; } = [];

    // Docker-era schema fields are retained only while reading v1/v2 files. Saving a
    // schema v3 configuration removes them so the normal runtime has no Docker contract.
    [JsonPropertyName("image_name")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LegacyImageName { get; set; }

    [JsonPropertyName("image_root")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LegacyImageRoot { get; set; }

    [JsonPropertyName("workspace_type")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LegacyWorkspaceType { get; set; }

    [JsonPropertyName("workspace_source")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LegacyWorkspaceSource { get; set; }

    [JsonIgnore]
    public LauncherLibrary? DefaultLibrary => GetLibrary(DefaultLibraryId) ??
        Libraries.FirstOrDefault(library => library.Enabled) ??
        Libraries.FirstOrDefault();

    [JsonIgnore]
    public IReadOnlyList<LauncherLibrary> EnabledLibraries => Libraries
        .Where(library => library.Enabled)
        .ToList();

    [JsonIgnore]
    public string ImageRoot => DefaultLibrary?.ImageRoot ?? LegacyImageRoot ?? string.Empty;

    [JsonIgnore]
    public string WorkspaceDirectory => DefaultLibrary?.WorkspaceDirectory ?? string.Empty;

    public LauncherLibrary? GetLibrary(string? libraryId) => Libraries.FirstOrDefault(
        library => string.Equals(
            library.Id,
            libraryId,
            StringComparison.OrdinalIgnoreCase
        )
    );
}

public sealed class LauncherLibrary
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = string.Empty;

    [JsonPropertyName("name")]
    public string Name { get; set; } = string.Empty;

    [JsonPropertyName("image_root")]
    public string ImageRoot { get; set; } = string.Empty;

    [JsonPropertyName("workspace_directory")]
    public string WorkspaceDirectory { get; set; } = string.Empty;

    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; } = true;

    // Schema v2 compatibility fields. They are cleared after migration.
    [JsonPropertyName("workspace_type")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LegacyWorkspaceType { get; set; }

    [JsonPropertyName("workspace_source")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LegacyWorkspaceSource { get; set; }

    [JsonIgnore]
    public bool HasLegacyNamedVolume => string.Equals(
        LegacyWorkspaceType,
        "volume",
        StringComparison.OrdinalIgnoreCase
    );

    public LauncherLibrary Copy() => new()
    {
        Id = Id,
        Name = Name,
        ImageRoot = ImageRoot,
        WorkspaceDirectory = WorkspaceDirectory,
        Enabled = Enabled,
        LegacyWorkspaceType = LegacyWorkspaceType,
        LegacyWorkspaceSource = LegacyWorkspaceSource,
    };
}
