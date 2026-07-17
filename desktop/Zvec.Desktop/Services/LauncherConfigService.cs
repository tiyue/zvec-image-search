using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

public sealed partial class LauncherConfigService
{
    private static readonly JsonSerializerOptions ReadOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    private static readonly JsonSerializerOptions WriteOptions = new()
    {
        WriteIndented = true,
    };

    private readonly Func<CancellationToken, Task>? _legacyMigration;

    public LauncherConfigService(Func<CancellationToken, Task>? legacyMigration = null)
    {
        _legacyMigration = legacyMigration;
    }

    public string ConfigHome
    {
        get
        {
            var configured = Environment.GetEnvironmentVariable("ZVEC_CONFIG_HOME");
            if (string.IsNullOrWhiteSpace(configured))
            {
                // Preserve existing installations while the public environment variable
                // transitions away from its Docker-era name.
                configured = Environment.GetEnvironmentVariable("ZVEC_DOCKER_CONFIG_HOME");
            }
            if (!string.IsNullOrWhiteSpace(configured))
            {
                return Path.GetFullPath(Environment.ExpandEnvironmentVariables(configured));
            }

            var localAppData = Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData
            );
            return Path.Combine(localAppData, "zvec-image-search");
        }
    }

    public string ConfigPath => Path.Combine(ConfigHome, "config.json");

    public string EnvironmentPath => Path.Combine(ConfigHome, ".env");

    public string DefaultWorkspacePath => Path.Combine(ConfigHome, "workspace");

    public string DefaultResultsPath => Path.Combine(ConfigHome, "results", "desktop");

    public void ValidateDraft(LauncherConfig config)
    {
        ArgumentNullException.ThrowIfNull(config);
        var copy = new LauncherConfig
        {
            SchemaVersion = config.SchemaVersion,
            PythonExecutable = config.PythonExecutable,
            ResultsDirectory = config.ResultsDirectory,
            DefaultLibraryId = config.DefaultLibraryId,
            Libraries = config.Libraries.Select(library => library.Copy()).ToList(),
        };
        NormalizeAndValidate(copy);
    }

    public async Task<LauncherConfigInspection> InspectAsync(
        CancellationToken cancellationToken = default)
    {
        if (!File.Exists(ConfigPath))
        {
            return new LauncherConfigInspection
            {
                SchemaVersion = LauncherConfig.CurrentSchemaVersion,
                DefaultBackupRoot = Path.Combine(ConfigHome, "migration-backups"),
                ApiRequests = 0,
            };
        }

        LauncherConfig config;
        await using (var stream = File.OpenRead(ConfigPath))
        {
            config = await JsonSerializer.DeserializeAsync<LauncherConfig>(
                stream,
                ReadOptions,
                cancellationToken
            ) ?? throw new InvalidDataException($"无法解析启动器配置：{ConfigPath}");
        }

        var namedVolumes = new List<LauncherNamedVolumeInspection>();
        var blockers = new List<string>();
        var defaultLibraryId = config.DefaultLibraryId;
        if (config.SchemaVersion == 1)
        {
            var imageRoot = config.LegacyImageRoot?.Trim() ?? string.Empty;
            var legacyWorkspaceSource =
                config.LegacyWorkspaceSource?.Trim() ?? string.Empty;
            defaultLibraryId = CreateMigratedLibraryId(
                imageRoot,
                legacyWorkspaceSource
            );
            if (string.Equals(
                config.LegacyWorkspaceType,
                "volume",
                StringComparison.OrdinalIgnoreCase
            ))
            {
                namedVolumes.Add(new LauncherNamedVolumeInspection
                {
                    LibraryId = defaultLibraryId,
                    LibraryName = GetLibraryName(imageRoot, "默认图库"),
                    VolumeName = legacyWorkspaceSource,
                    DefaultExportDirectory = Path.Combine(
                        ConfigHome,
                        "libraries",
                        defaultLibraryId,
                        "workspace"
                    ),
                });
            }
        }
        else if (config.SchemaVersion == 2)
        {
            foreach (var library in config.Libraries.Where(
                library => library.HasLegacyNamedVolume
            ))
            {
                var configuredId = library.Id.Trim().ToLowerInvariant();
                var libraryId = LibraryIdPattern().IsMatch(configuredId)
                    ? configuredId
                    : CreateMigratedLibraryId(
                        library.ImageRoot,
                        library.LegacyWorkspaceSource ?? string.Empty
                    );
                if (!LibraryIdPattern().IsMatch(configuredId))
                {
                    blockers.Add($"旧图库 ID 不合法，迁移前必须修复：{library.Id}");
                }
                namedVolumes.Add(new LauncherNamedVolumeInspection
                {
                    LibraryId = libraryId,
                    LibraryName = string.IsNullOrWhiteSpace(library.Name)
                        ? library.Id
                        : library.Name,
                    VolumeName = library.LegacyWorkspaceSource?.Trim() ?? string.Empty,
                    DefaultExportDirectory = Path.Combine(
                        ConfigHome,
                        "libraries",
                        libraryId,
                        "workspace"
                    ),
                });
            }
        }
        if (namedVolumes.Count > 0)
        {
            blockers.Add(
                "旧配置包含 Docker named volume；必须由用户确认后执行一次性导出。"
            );
        }

        var selected = config.GetLibrary(config.DefaultLibraryId) ??
            config.Libraries.FirstOrDefault();
        var workspaceType = config.SchemaVersion == 1
            ? config.LegacyWorkspaceType
            : selected?.LegacyWorkspaceType;
        var workspaceSource = config.SchemaVersion == 1
            ? config.LegacyWorkspaceSource
            : selected?.LegacyWorkspaceSource;
        var hasNamedVolume = string.Equals(
            workspaceType,
            "volume",
            StringComparison.OrdinalIgnoreCase
        ) || config.Libraries.Any(library => library.HasLegacyNamedVolume);
        return new LauncherConfigInspection
        {
            Exists = true,
            SchemaVersion = config.SchemaVersion,
            HasNamedVolume = namedVolumes.Count > 0 || hasNamedVolume,
            DefaultBackupRoot = Path.Combine(ConfigHome, "migration-backups"),
            NamedVolumes = namedVolumes,
            Blockers = blockers,
            ApiRequests = 0,
            DefaultLibraryId = defaultLibraryId,
            LibraryName = selected?.Name ?? (config.SchemaVersion == 1 ? "默认图库" : null),
            ImageRoot = config.SchemaVersion == 1
                ? config.LegacyImageRoot
                : selected?.ImageRoot,
            WorkspaceType = workspaceType,
            WorkspaceSource = workspaceSource,
            ResultsDirectory = config.ResultsDirectory,
        };
    }

    public async Task<LauncherConfig?> LoadAsync(CancellationToken cancellationToken = default)
    {
        if (!File.Exists(ConfigPath))
        {
            return null;
        }

        LauncherConfig config;
        await using (var stream = File.OpenRead(ConfigPath))
        {
            config = await JsonSerializer.DeserializeAsync<LauncherConfig>(
                stream,
                ReadOptions,
                cancellationToken
            ) ?? throw new InvalidDataException($"无法解析启动器配置：{ConfigPath}");
        }

        if (config.SchemaVersion is 1 or 2)
        {
            EnsureLegacyConfigHasNoNamedVolume(config);
            if (_legacyMigration is not null)
            {
                await _legacyMigration(cancellationToken);
            }
            else
            {
                await RunNativeLegacyMigrationAsync(cancellationToken);
            }
            await using var migratedStream = File.OpenRead(ConfigPath);
            config = await JsonSerializer.DeserializeAsync<LauncherConfig>(
                migratedStream,
                ReadOptions,
                cancellationToken
            ) ?? throw new InvalidDataException(
                $"迁移工具没有写回有效配置：{ConfigPath}"
            );
            if (config.SchemaVersion != LauncherConfig.CurrentSchemaVersion)
            {
                throw new InvalidDataException(
                    "旧配置迁移未完成；原配置和 Workspace 备份已保留，请查看迁移日志。"
                );
            }
            NormalizeAndValidate(config);
        }
        else
        {
            NormalizeAndValidate(config);
        }

        return config;
    }

    private async Task RunNativeLegacyMigrationAsync(
        CancellationToken cancellationToken)
    {
        var repositoryRoot = RepositoryLocator.FindRepositoryRoot();
        using var runner = new PowerShellZvecRunner(repositoryRoot);
        var migration = new WorkspaceMigrationService(runner);
        var response = await migration.MigrateAsync(
            new WorkspaceMigrationRequest(),
            cancellationToken
        );
        if (!response.IsSuccess)
        {
            throw new InvalidDataException(
                "旧配置迁移失败。原配置未被覆盖。" + Environment.NewLine +
                response.Command.CombinedOutput.Trim()
            );
        }
        if (response.Report.ApiRequests != 0)
        {
            throw new InvalidDataException(
                "迁移报告异常：已有向量迁移不得调用模型 API。"
            );
        }
    }

    private static void EnsureLegacyConfigHasNoNamedVolume(LauncherConfig config)
    {
        if (
            config.SchemaVersion == 1 &&
            string.Equals(
                config.LegacyWorkspaceType,
                "volume",
                StringComparison.OrdinalIgnoreCase
            )
        )
        {
            EnsureLegacyWorkspaceIsHostDirectory(
                config.LegacyWorkspaceType ?? string.Empty,
                config.LegacyWorkspaceSource ?? string.Empty,
                "默认图库"
            );
        }
        foreach (var library in config.Libraries.Where(library => library.HasLegacyNamedVolume))
        {
            EnsureLegacyWorkspaceIsHostDirectory(
                library.LegacyWorkspaceType ?? string.Empty,
                library.LegacyWorkspaceSource ?? string.Empty,
                string.IsNullOrWhiteSpace(library.Name) ? library.Id : library.Name
            );
        }
    }

    public async Task SaveAsync(
        LauncherConfig config,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(config);
        config.SchemaVersion = LauncherConfig.CurrentSchemaVersion;
        config.LegacyImageName = null;
        config.LegacyImageRoot = null;
        config.LegacyWorkspaceType = null;
        config.LegacyWorkspaceSource = null;
        foreach (var library in config.Libraries)
        {
            library.LegacyWorkspaceType = null;
            library.LegacyWorkspaceSource = null;
        }
        NormalizeAndValidate(config);

        Directory.CreateDirectory(ConfigHome);
        var temporaryPath = Path.Combine(
            ConfigHome,
            $"config.{Guid.NewGuid():N}.tmp"
        );
        try
        {
            await using (var stream = new FileStream(
                temporaryPath,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                bufferSize: 16 * 1024,
                FileOptions.Asynchronous | FileOptions.WriteThrough
            ))
            {
                await JsonSerializer.SerializeAsync(
                    stream,
                    config,
                    WriteOptions,
                    cancellationToken
                );
                await stream.FlushAsync(cancellationToken);
            }
            File.Move(temporaryPath, ConfigPath, overwrite: true);
        }
        finally
        {
            TryDeleteFile(temporaryPath);
        }
    }

    public LauncherLibrary CreateLibraryTemplate(string? name = null)
    {
        var id = $"lib-{Guid.NewGuid():N}";
        return new LauncherLibrary
        {
            Id = id,
            Name = string.IsNullOrWhiteSpace(name) ? "新图库" : name.Trim(),
            WorkspaceDirectory = Path.Combine(ConfigHome, "libraries", id, "workspace"),
            Enabled = true,
        };
    }

    public bool HasStoredApiKey()
    {
        if (!File.Exists(EnvironmentPath))
        {
            return false;
        }

        return File.ReadLines(EnvironmentPath).Any(
            line => line.StartsWith("DASHSCOPE_API_KEY=", StringComparison.Ordinal) &&
                line.Length > "DASHSCOPE_API_KEY=".Length
        );
    }

    public string? ReadLegacyApiKey()
    {
        return ReadLegacyEnvironmentValue("DASHSCOPE_API_KEY");
    }

    public string? ReadLegacyApiUrl() => ReadLegacyEnvironmentValue("DASHSCOPE_API_URL");

    public void RemoveLegacyApiKey()
    {
        if (!File.Exists(EnvironmentPath))
        {
            return;
        }
        var remaining = File.ReadAllLines(EnvironmentPath)
            .Where(line => !line.StartsWith("DASHSCOPE_API_KEY=", StringComparison.Ordinal))
            .Where(line => !string.IsNullOrWhiteSpace(line))
            .ToArray();
        if (remaining.Length == 0)
        {
            File.Delete(EnvironmentPath);
            return;
        }

        var temporaryPath = EnvironmentPath + $".{Guid.NewGuid():N}.tmp";
        try
        {
            File.WriteAllText(
                temporaryPath,
                string.Join(Environment.NewLine, remaining) + Environment.NewLine,
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false)
            );
            File.Move(temporaryPath, EnvironmentPath, overwrite: true);
        }
        finally
        {
            TryDeleteFile(temporaryPath);
        }
    }

    private static void EnsureLegacyWorkspaceIsHostDirectory(
        string workspaceType,
        string workspaceSource,
        string libraryName)
    {
        if (string.Equals(workspaceType, "bind", StringComparison.OrdinalIgnoreCase))
        {
            return;
        }
        if (string.Equals(workspaceType, "volume", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException(
                $"图库“{libraryName}”仍使用 Docker named volume “{workspaceSource}”。" +
                "原生后端不能直接打开 named volume；请先使用迁移工具把该 volume " +
                "导出到宿主机目录，再把 workspace_directory 指向导出目录。"
            );
        }
        throw new InvalidDataException(
            $"图库“{libraryName}”的旧 workspace_type 不受支持：{workspaceType}"
        );
    }

    private static void NormalizeAndValidate(LauncherConfig config)
    {
        if (config.SchemaVersion != LauncherConfig.CurrentSchemaVersion)
        {
            throw new InvalidDataException(
                $"不支持的启动器配置版本：{config.SchemaVersion}"
            );
        }
        config.PythonExecutable = string.IsNullOrWhiteSpace(config.PythonExecutable)
            ? null
            : config.PythonExecutable.Trim();
        config.ResultsDirectory = RequireValue(
            config.ResultsDirectory,
            "results_directory"
        );
        if (config.Libraries.Count == 0)
        {
            throw new InvalidDataException("启动器配置至少需要一个图库。");
        }

        var ids = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var names = new HashSet<string>(StringComparer.CurrentCultureIgnoreCase);
        var workspaces = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var library in config.Libraries)
        {
            library.Id = RequireValue(library.Id, "libraries[].id").ToLowerInvariant();
            if (!LibraryIdPattern().IsMatch(library.Id))
            {
                throw new InvalidDataException($"图库 ID 不合法：{library.Id}");
            }
            if (!ids.Add(library.Id))
            {
                throw new InvalidDataException($"图库 ID 重复：{library.Id}");
            }

            library.Name = RequireValue(library.Name, $"libraries[{library.Id}].name");
            if (!names.Add(library.Name))
            {
                throw new InvalidDataException($"图库名称重复：{library.Name}");
            }
            library.ImageRoot = RequireValue(
                library.ImageRoot,
                $"libraries[{library.Id}].image_root"
            );
            library.WorkspaceDirectory = RequireValue(
                library.WorkspaceDirectory,
                $"libraries[{library.Id}].workspace_directory"
            );
            var workspaceIdentity = CanonicalizeDirectoryPath(
                library.WorkspaceDirectory
            );
            if (!workspaces.Add(workspaceIdentity))
            {
                throw new InvalidDataException(
                    $"多个图库不能共用同一个 Workspace：{library.WorkspaceDirectory}"
                );
            }
        }

        var resultsPath = CanonicalizeDirectoryPath(config.ResultsDirectory);
        var imageRoots = config.Libraries
            .Select(library => new
            {
                library.Name,
                Path = CanonicalizeDirectoryPath(library.ImageRoot),
            })
            .ToList();
        var workspacesByLibrary = config.Libraries
            .Select(library => new
            {
                library.Name,
                Path = CanonicalizeDirectoryPath(library.WorkspaceDirectory),
            })
            .ToList();

        foreach (var imageRoot in imageRoots)
        {
            if (PathsOverlap(resultsPath, imageRoot.Path))
            {
                throw new InvalidDataException(
                    $"结果目录不能与图库“{imageRoot.Name}”的图片目录重叠。"
                );
            }
        }
        foreach (var workspace in workspacesByLibrary)
        {
            if (PathsOverlap(resultsPath, workspace.Path))
            {
                throw new InvalidDataException(
                    $"结果目录不能与图库“{workspace.Name}”的 Workspace 重叠。"
                );
            }
            foreach (var imageRoot in imageRoots)
            {
                if (PathsOverlap(workspace.Path, imageRoot.Path))
                {
                    throw new InvalidDataException(
                        $"图库“{workspace.Name}”的 Workspace 不能与图库“{imageRoot.Name}”的图片目录重叠。"
                    );
                }
            }
        }
        for (var left = 0; left < workspacesByLibrary.Count; left++)
        {
            for (var right = left + 1; right < workspacesByLibrary.Count; right++)
            {
                if (PathsOverlap(
                    workspacesByLibrary[left].Path,
                    workspacesByLibrary[right].Path
                ))
                {
                    throw new InvalidDataException(
                        $"图库“{workspacesByLibrary[left].Name}”与“{workspacesByLibrary[right].Name}”的 Workspace 不能重叠。"
                    );
                }
            }
        }

        if (string.IsNullOrWhiteSpace(config.DefaultLibraryId))
        {
            config.DefaultLibraryId = config.Libraries[0].Id;
        }
        config.DefaultLibraryId = config.DefaultLibraryId.Trim().ToLowerInvariant();
        if (!ids.Contains(config.DefaultLibraryId))
        {
            throw new InvalidDataException(
                $"默认图库不存在：{config.DefaultLibraryId}"
            );
        }
        if (config.GetLibrary(config.DefaultLibraryId)?.Enabled != true)
        {
            throw new InvalidDataException("默认图库必须处于启用状态。");
        }
    }

    private static string RequireValue(string? value, string fieldName)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new InvalidDataException($"启动器配置缺少字段：{fieldName}");
        }
        return value.Trim();
    }

    private static string CreateMigratedLibraryId(
        string imageRoot,
        string workspaceSource)
    {
        var identity = string.Join(
            '\0',
            imageRoot.Trim().ToUpperInvariant(),
            workspaceSource.Trim().ToUpperInvariant()
        );
        var hash = Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(identity))
        ).ToLowerInvariant();
        return $"lib-{hash[..20]}";
    }

    private static string GetLibraryName(string imageRoot, string fallback)
    {
        var name = Path.GetFileName(
            imageRoot.TrimEnd(
                Path.DirectorySeparatorChar,
                Path.AltDirectorySeparatorChar
            )
        );
        return string.IsNullOrWhiteSpace(name) ? fallback : name;
    }

    private static string CanonicalizeDirectoryPath(string path)
    {
        var fullPath = Path.GetFullPath(
            Environment.ExpandEnvironmentVariables(path.Trim())
        );
        var root = Path.GetPathRoot(fullPath) ?? string.Empty;
        var current = root;
        var remainder = fullPath[root.Length..];
        foreach (var segment in remainder.Split(
            [Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar],
            StringSplitOptions.RemoveEmptyEntries
        ))
        {
            var candidate = Path.Combine(current, segment);
            if (Directory.Exists(candidate))
            {
                try
                {
                    var target = new DirectoryInfo(candidate).ResolveLinkTarget(
                        returnFinalTarget: true
                    );
                    current = target?.FullName ?? candidate;
                }
                catch (UnauthorizedAccessException)
                {
                    // Some constrained Windows environments permit using a
                    // directory while denying link-metadata access to one of
                    // its ancestors. Keep the normalized lexical path instead
                    // of rejecting an otherwise usable library location.
                    current = candidate;
                }
            }
            else
            {
                current = candidate;
            }
        }
        return Path.TrimEndingDirectorySeparator(Path.GetFullPath(current));
    }

    private static bool PathsOverlap(string left, string right) =>
        IsSameOrDescendant(left, right) || IsSameOrDescendant(right, left);

    private static bool IsSameOrDescendant(string candidate, string parent)
    {
        if (string.Equals(candidate, parent, StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }
        var prefix = Path.EndsInDirectorySeparator(parent)
            ? parent
            : parent + Path.DirectorySeparatorChar;
        return candidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase);
    }

    private string? ReadLegacyEnvironmentValue(string name)
    {
        if (!File.Exists(EnvironmentPath))
        {
            return null;
        }
        var prefix = name + "=";
        var line = File.ReadLines(EnvironmentPath).FirstOrDefault(
            value => value.StartsWith(prefix, StringComparison.Ordinal)
        );
        return line is null || line.Length == prefix.Length
            ? null
            : line[prefix.Length..];
    }

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

    [GeneratedRegex("^[a-z0-9][a-z0-9_-]{2,63}$", RegexOptions.CultureInvariant)]
    private static partial Regex LibraryIdPattern();
}
