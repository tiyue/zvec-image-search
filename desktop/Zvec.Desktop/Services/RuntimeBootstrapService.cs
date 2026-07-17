using System.Text.Json;
using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

public interface IRuntimeBootstrapCommandRunner
{
    Task<CommandResult> RunZvecAsync(
        string command,
        IEnumerable<string>? arguments,
        IReadOnlyDictionary<string, string?>? environment,
        CancellationToken cancellationToken);
}

public interface IRuntimeBootstrapper
{
    Task<RuntimeBootstrapResult> EnsureReadyAsync(
        string? bootstrapPython = null,
        CancellationToken cancellationToken = default);
}

public sealed class RuntimeBootstrapService : IDisposable, IRuntimeBootstrapper
{
    internal const string ResultPrefix = "@@ZVEC_RUNTIME_RESULT@@";
    private const int SupportedSchemaVersion = 1;
    private static readonly string[] RequiredDependencies = ["zvec", "Pillow", "numpy"];
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    private readonly IRuntimeBootstrapCommandRunner _runner;
    private readonly IDisposable? _ownedRunner;
    private bool _disposed;

    public RuntimeBootstrapService(string repositoryRoot)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(repositoryRoot);
        var root = Path.GetFullPath(repositoryRoot);
        var scriptPath = Path.Combine(root, "scripts", "zvec.ps1");
        if (!File.Exists(scriptPath))
        {
            throw new FileNotFoundException(
                "找不到原生运行环境修复脚本 scripts/zvec.ps1。请修复或重新安装完整桌面包。",
                scriptPath
            );
        }

        var runner = new PowerShellZvecRunner(root);
        _runner = runner;
        _ownedRunner = runner;
    }

    internal RuntimeBootstrapService(IRuntimeBootstrapCommandRunner runner)
    {
        _runner = runner ?? throw new ArgumentNullException(nameof(runner));
    }

    public Task<RuntimeBootstrapResult> EnsureReadyAsync(
        string? bootstrapPython = null,
        CancellationToken cancellationToken = default) => RunAsync(
            "runtime-bootstrap",
            bootstrapPython,
            cancellationToken
        );

    public Task<RuntimeBootstrapResult> DiagnoseAsync(
        string? bootstrapPython = null,
        CancellationToken cancellationToken = default) => RunAsync(
            "runtime-doctor",
            bootstrapPython,
            cancellationToken
        );

    public async Task<RuntimeBootstrapResult> EnsureReadyOrThrowAsync(
        string? bootstrapPython = null,
        CancellationToken cancellationToken = default)
    {
        var result = await EnsureReadyAsync(bootstrapPython, cancellationToken);
        if (result.IsSuccess)
        {
            return result;
        }
        cancellationToken.ThrowIfCancellationRequested();
        throw new RuntimeBootstrapException(result);
    }

    private async Task<RuntimeBootstrapResult> RunAsync(
        string command,
        string? bootstrapPython,
        CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        var environment = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase);
        if (!string.IsNullOrWhiteSpace(bootstrapPython))
        {
            environment["ZVEC_PYTHON"] = bootstrapPython.Trim();
        }

        try
        {
            var commandResult = await _runner.RunZvecAsync(
                command,
                arguments: null,
                environment,
                cancellationToken
            );
            if (commandResult.WasCancelled)
            {
                return CreateFailure(
                    "cancelled",
                    "cancelled",
                    "运行环境准备已取消。",
                    "重新点击“修复运行环境”即可继续。",
                    commandResult.CombinedOutput
                );
            }
            return ParseCommandResult(commandResult);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            return CreateFailure(
                "cancelled",
                "cancelled",
                "运行环境准备已取消。",
                "重新点击“修复运行环境”即可继续。"
            );
        }
        catch (Exception exception) when (
            exception is InvalidOperationException or System.ComponentModel.Win32Exception or
                IOException or UnauthorizedAccessException
        )
        {
            return CreateFailure(
                "failed",
                "bootstrap_process_failed",
                $"无法启动运行环境准备流程：{exception.Message}",
                "确认 PowerShell 可用，并修复或重新安装完整桌面包后重试。",
                exception.ToString()
            );
        }
    }

    internal static RuntimeBootstrapResult ParseCommandResult(CommandResult commandResult)
    {
        ArgumentNullException.ThrowIfNull(commandResult);
        var resultLine = commandResult.StandardOutput
            .Split(["\r\n", "\n"], StringSplitOptions.RemoveEmptyEntries)
            .LastOrDefault(line => line.StartsWith(ResultPrefix, StringComparison.Ordinal));
        if (resultLine is null)
        {
            return CreateFailure(
                "failed",
                "bootstrap_response_missing",
                "运行环境准备流程未返回可识别的诊断结果。",
                "打开安装日志；若问题持续，请修复或重新安装桌面应用。",
                commandResult.CombinedOutput
            );
        }

        RuntimeBootstrapResult? parsed;
        try
        {
            parsed = JsonSerializer.Deserialize<RuntimeBootstrapResult>(
                resultLine[ResultPrefix.Length..],
                JsonOptions
            );
        }
        catch (JsonException exception)
        {
            return CreateFailure(
                "failed",
                "bootstrap_response_invalid",
                $"运行环境诊断结果格式错误：{exception.Message}",
                "修复或重新安装桌面应用后重试。",
                commandResult.CombinedOutput
            );
        }

        if (parsed is null || parsed.SchemaVersion != SupportedSchemaVersion ||
            string.IsNullOrWhiteSpace(parsed.Status) ||
            string.IsNullOrWhiteSpace(parsed.Code) ||
            string.IsNullOrWhiteSpace(parsed.Message))
        {
            return CreateFailure(
                "failed",
                "bootstrap_response_invalid",
                "运行环境诊断结果缺少必要字段或版本不兼容。",
                "请更新或重新安装与当前桌面端匹配的完整应用包。",
                commandResult.CombinedOutput
            );
        }

        if (parsed.Success && commandResult.ExitCode != 0)
        {
            return CreateFailure(
                "failed",
                "bootstrap_exit_code_mismatch",
                $"运行环境报告成功，但准备进程退出码为 {commandResult.ExitCode}。",
                "打开安装日志并重新执行运行环境修复。",
                commandResult.CombinedOutput
            );
        }
        if (!parsed.Success && commandResult.ExitCode == 0)
        {
            return CreateFailure(
                "failed",
                "bootstrap_exit_code_mismatch",
                "运行环境报告失败，但准备进程错误地返回了成功退出码。",
                "更新或重新安装桌面应用后重试。",
                commandResult.CombinedOutput
            );
        }
        if (parsed.Success && !HasRequiredRuntimeDetails(parsed))
        {
            return CreateFailure(
                "failed",
                "bootstrap_verification_incomplete",
                "运行环境没有完整验证 Python、zvec、Pillow 和 numpy。",
                "重新执行运行环境修复；若仍失败，请打开安装日志。",
                commandResult.CombinedOutput
            );
        }

        return parsed with { DiagnosticOutput = commandResult.CombinedOutput.Trim() };
    }

    private static bool HasRequiredRuntimeDetails(RuntimeBootstrapResult result)
    {
        if (string.IsNullOrWhiteSpace(result.PythonExecutable) ||
            string.IsNullOrWhiteSpace(result.PythonVersion) ||
            string.IsNullOrWhiteSpace(result.PythonArchitecture))
        {
            return false;
        }
        return RequiredDependencies.All(required => result.Dependencies.Any(
            dependency => dependency.Imported &&
                string.Equals(dependency.Name, required, StringComparison.OrdinalIgnoreCase) &&
                !string.IsNullOrWhiteSpace(dependency.Version)
        ));
    }

    private static RuntimeBootstrapResult CreateFailure(
        string status,
        string code,
        string message,
        string recommendedAction,
        string diagnosticOutput = "") => new()
        {
            SchemaVersion = SupportedSchemaVersion,
            Success = false,
            Status = status,
            Code = code,
            Message = message,
            RecommendedAction = recommendedAction,
            DiagnosticOutput = diagnosticOutput.Trim(),
        };

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _ownedRunner?.Dispose();
        GC.SuppressFinalize(this);
    }
}
