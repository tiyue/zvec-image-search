using System.Text.Json;
using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

public interface IZvecCommandRunner
{
    Task<CommandResult> RunZvecAsync(
        string command,
        IEnumerable<string>? arguments = null,
        IReadOnlyDictionary<string, string?>? environment = null,
        CancellationToken cancellationToken = default);
}

public sealed class WorkspaceMigrationService(IZvecCommandRunner runner)
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    public Task<WorkspaceCommandResponse<WorkspaceBackupReport>> PlanBackupAsync(
        WorkspaceBackupRequest request,
        CancellationToken cancellationToken = default) =>
        RunAsync<WorkspaceBackupReport>(
            "workspace-backup",
            BuildBackupArguments(request, dryRun: true),
            cancellationToken
        );

    public Task<WorkspaceCommandResponse<WorkspaceBackupReport>> CreateBackupAsync(
        WorkspaceBackupRequest request,
        CancellationToken cancellationToken = default) =>
        RunAsync<WorkspaceBackupReport>(
            "workspace-backup",
            BuildBackupArguments(request, dryRun: false),
            cancellationToken
        );

    public Task<WorkspaceCommandResponse<WorkspaceMigrationReport>> PlanMigrationAsync(
        WorkspaceMigrationRequest request,
        CancellationToken cancellationToken = default) =>
        RunAsync<WorkspaceMigrationReport>(
            "migrate-docker-workspace",
            BuildMigrationArguments(request, dryRun: true),
            cancellationToken
        );

    public Task<WorkspaceCommandResponse<WorkspaceMigrationReport>> MigrateAsync(
        WorkspaceMigrationRequest request,
        CancellationToken cancellationToken = default) =>
        RunAsync<WorkspaceMigrationReport>(
            "migrate-docker-workspace",
            BuildMigrationArguments(request, dryRun: false),
            cancellationToken
        );

    private async Task<WorkspaceCommandResponse<T>> RunAsync<T>(
        string command,
        IReadOnlyList<string> arguments,
        CancellationToken cancellationToken)
    {
        var result = await runner.RunZvecAsync(
            command,
            arguments,
            cancellationToken: cancellationToken
        );
        var json = ExtractJson(result.StandardOutput);
        if (json is null)
        {
            throw new InvalidOperationException(
                $"迁移工具没有返回可解析的 JSON。{Environment.NewLine}" +
                result.CombinedOutput.Trim()
            );
        }
        var report = JsonSerializer.Deserialize<T>(json, JsonOptions) ??
            throw new InvalidDataException("迁移工具返回了空 JSON 对象。");
        return new WorkspaceCommandResponse<T>(report, result);
    }

    private static List<string> BuildBackupArguments(
        WorkspaceBackupRequest request,
        bool dryRun)
    {
        ArgumentNullException.ThrowIfNull(request);
        var arguments = new List<string>();
        AddOption(arguments, "--library", request.LibraryId);
        AddOption(arguments, "--destination", request.Destination);
        if (request.FullBackup)
        {
            arguments.Add("--full");
        }
        if (dryRun)
        {
            arguments.Add("--dry-run");
        }
        return arguments;
    }

    private static List<string> BuildMigrationArguments(
        WorkspaceMigrationRequest request,
        bool dryRun)
    {
        ArgumentNullException.ThrowIfNull(request);
        var arguments = new List<string>();
        AddOption(arguments, "--library", request.LibraryId);
        AddOption(arguments, "--destination", request.ExportDestination);
        AddOption(arguments, "--backup-directory", request.BackupDirectory);
        if (request.FullBackup)
        {
            arguments.Add("--full-backup");
        }
        if (dryRun)
        {
            arguments.Add("--dry-run");
        }
        return arguments;
    }

    private static void AddOption(
        List<string> arguments,
        string option,
        string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return;
        }
        arguments.Add(option);
        arguments.Add(value.Trim());
    }

    internal static string? ExtractJson(string output)
    {
        if (string.IsNullOrWhiteSpace(output))
        {
            return null;
        }
        var trimmed = output.Trim();
        for (var index = 0; index < trimmed.Length; index++)
        {
            if (trimmed[index] != '{')
            {
                continue;
            }
            var candidate = trimmed[index..];
            try
            {
                using var document = JsonDocument.Parse(candidate);
                return document.RootElement.GetRawText();
            }
            catch (JsonException)
            {
                // Bootstrap logs may precede the final JSON object. Continue at the
                // next opening brace until a complete trailing document is found.
            }
        }
        return null;
    }
}
