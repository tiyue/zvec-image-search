using System.Diagnostics;
using System.Text;
using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

public sealed class PowerShellZvecRunner :
    IDisposable,
    IRuntimeBootstrapCommandRunner,
    IZvecCommandRunner
{
    private readonly string _repositoryRoot;
    private readonly string _scriptPath;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly object _stateLock = new();
    private Process? _currentProcess;
    private bool _cancelRequested;
    private bool _disposed;

    public PowerShellZvecRunner(string repositoryRoot)
    {
        _repositoryRoot = repositoryRoot;
        _scriptPath = Path.Combine(repositoryRoot, "scripts", "zvec.ps1");
    }

    public event EventHandler<CommandOutputEventArgs>? OutputReceived;

    public bool IsRunning
    {
        get
        {
            lock (_stateLock)
            {
                return _currentProcess is { HasExited: false };
            }
        }
    }

    public Task<CommandResult> RunZvecAsync(
        string command,
        IEnumerable<string>? arguments = null,
        IReadOnlyDictionary<string, string?>? environment = null,
        CancellationToken cancellationToken = default)
    {
        var processArguments = new List<string>
        {
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            _scriptPath,
            command,
        };
        if (arguments is not null)
        {
            processArguments.AddRange(arguments);
        }

        var mergedEnvironment = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase)
        {
            ["ZVEC_UTF8_OUTPUT"] = "1",
            ["PYTHONIOENCODING"] = "utf-8",
            ["PYTHONUTF8"] = "1",
        };
        if (environment is not null)
        {
            foreach (var (key, value) in environment)
            {
                mergedEnvironment[key] = value;
            }
        }
        if (
            string.Equals(command, "init", StringComparison.OrdinalIgnoreCase) &&
            arguments?.Contains("--skip-key", StringComparer.OrdinalIgnoreCase) == true
        )
        {
            // `--skip-key` is used after the GUI has stored the secret in Windows
            // Credential Manager. Never let an inherited process environment make
            // the launcher persist that secret back to its legacy .env file.
            mergedEnvironment["DASHSCOPE_API_KEY"] = null;
        }

        return RunProcessAsync(
            ResolvePowerShell(),
            processArguments,
            mergedEnvironment,
            cancellationToken
        );
    }

    public async Task CancelCurrentAsync()
    {
        Process? process;
        lock (_stateLock)
        {
            _cancelRequested = true;
            process = _currentProcess;
        }

        // The native CLI and any child interpreter are terminated as one process tree.
        await ProcessTermination.KillTreeAndWaitAsync(process, TimeSpan.FromSeconds(1));
    }

    private async Task<CommandResult> RunProcessAsync(
        string fileName,
        IReadOnlyList<string> arguments,
        IReadOnlyDictionary<string, string?>? environment,
        CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _gate.WaitAsync(cancellationToken);
        try
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = fileName,
                WorkingDirectory = _repositoryRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            foreach (var argument in arguments)
            {
                startInfo.ArgumentList.Add(argument);
            }
            if (environment is not null)
            {
                foreach (var (key, value) in environment)
                {
                    if (value is null)
                    {
                        startInfo.Environment.Remove(key);
                    }
                    else
                    {
                        startInfo.Environment[key] = value;
                    }
                }
            }

            using var process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
            var stdout = new StringBuilder();
            var stderr = new StringBuilder();
            var stdoutClosed = new TaskCompletionSource(
                TaskCreationOptions.RunContinuationsAsynchronously
            );
            var stderrClosed = new TaskCompletionSource(
                TaskCreationOptions.RunContinuationsAsynchronously
            );

            process.OutputDataReceived += (_, eventArgs) =>
            {
                if (eventArgs.Data is null)
                {
                    stdoutClosed.TrySetResult();
                    return;
                }

                lock (stdout)
                {
                    stdout.AppendLine(eventArgs.Data);
                }
                OutputReceived?.Invoke(this, new CommandOutputEventArgs(eventArgs.Data, false));
            };
            process.ErrorDataReceived += (_, eventArgs) =>
            {
                if (eventArgs.Data is null)
                {
                    stderrClosed.TrySetResult();
                    return;
                }

                lock (stderr)
                {
                    stderr.AppendLine(eventArgs.Data);
                }
                OutputReceived?.Invoke(this, new CommandOutputEventArgs(eventArgs.Data, true));
            };

            if (!process.Start())
            {
                throw new InvalidOperationException($"无法启动进程：{fileName}");
            }
            lock (_stateLock)
            {
                _currentProcess = process;
                _cancelRequested = false;
            }
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();

            using var registration = cancellationToken.Register(
                () => _ = CancelCurrentAsync()
            );
            await process.WaitForExitAsync();
            await Task.WhenAll(stdoutClosed.Task, stderrClosed.Task).WaitAsync(
                TimeSpan.FromSeconds(3)
            );

            bool wasCancelled;
            lock (_stateLock)
            {
                wasCancelled = _cancelRequested || cancellationToken.IsCancellationRequested;
            }
            return new CommandResult(
                process.ExitCode,
                stdout.ToString(),
                stderr.ToString(),
                wasCancelled
            );
        }
        finally
        {
            lock (_stateLock)
            {
                _currentProcess = null;
            }
            _gate.Release();
        }
    }

    private static string ResolvePowerShell()
    {
        var path = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        foreach (var directory in path.Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(directory))
            {
                continue;
            }

            var candidate = Path.Combine(directory.Trim(), "pwsh.exe");
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        return "powershell.exe";
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _gate.Dispose();
    }
}
