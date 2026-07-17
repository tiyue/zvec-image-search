using System.ComponentModel;
using System.Diagnostics;
using System.Text;

namespace Zvec.Desktop.Services;

internal sealed record BackendCommandResult(
    int ExitCode,
    string StandardOutput,
    string StandardError
);

internal interface IBackendCommandRunner
{
    Task<BackendCommandResult> RunAsync(
        ProcessStartInfo startInfo,
        TimeSpan timeout,
        CancellationToken cancellationToken);
}

internal sealed class BackendCommandRunner : IBackendCommandRunner
{
    public async Task<BackendCommandResult> RunAsync(
        ProcessStartInfo startInfo,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(startInfo);
        if (timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(timeout));
        }

        using var process = new Process { StartInfo = startInfo };
        if (!process.Start())
        {
            throw new InvalidOperationException(
                $"Could not start executable '{startInfo.FileName}'."
            );
        }

        var standardOutput = process.StandardOutput.ReadToEndAsync();
        var standardError = process.StandardError.ReadToEndAsync();
        using var commandTimeout = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken
        );
        commandTimeout.CancelAfter(timeout);
        try
        {
            await process.WaitForExitAsync(commandTimeout.Token);
        }
        catch
        {
            await ProcessTermination.KillTreeAndWaitAsync(
                process,
                TimeSpan.FromSeconds(1)
            );
            throw;
        }

        return new BackendCommandResult(
            process.ExitCode,
            await standardOutput,
            await standardError
        );
    }
}

internal interface IBackendProcess : IAsyncDisposable
{
    bool HasExited { get; }

    int? ExitCode { get; }

    int? ProcessId { get; }

    DateTimeOffset? StartTimeUtc { get; }

    string RecentOutput { get; }

    Task StartAsync(ProcessStartInfo startInfo, CancellationToken cancellationToken);

    Task StopAsync(TimeSpan timeout);
}

internal interface IBackendProcessFactory
{
    IBackendProcess Create();
}

internal sealed class BackendProcessFactory : IBackendProcessFactory
{
    public IBackendProcess Create() => new BackendProcess();
}

internal sealed class BackendProcess : IBackendProcess
{
    private const int MaximumCapturedCharacters = 32 * 1024;
    private readonly object _outputLock = new();
    private readonly StringBuilder _output = new();
    private Process? _process;
    private Task _standardOutputPump = Task.CompletedTask;
    private Task _standardErrorPump = Task.CompletedTask;
    private bool _disposed;

    public bool HasExited
    {
        get
        {
            var process = _process;
            if (process is null)
            {
                return true;
            }
            try
            {
                return process.HasExited;
            }
            catch (InvalidOperationException)
            {
                return true;
            }
        }
    }

    public int? ExitCode
    {
        get
        {
            var process = _process;
            if (process is null || !HasExited)
            {
                return null;
            }
            try
            {
                return process.ExitCode;
            }
            catch (InvalidOperationException)
            {
                return null;
            }
        }
    }

    public int? ProcessId
    {
        get
        {
            try
            {
                return _process?.Id;
            }
            catch (InvalidOperationException)
            {
                return null;
            }
        }
    }

    public DateTimeOffset? StartTimeUtc
    {
        get
        {
            var process = _process;
            if (process is null)
            {
                return null;
            }
            try
            {
                return process.StartTime.ToUniversalTime();
            }
            catch (Exception exception) when (
                exception is InvalidOperationException or NotSupportedException
            )
            {
                return null;
            }
        }
    }

    public string RecentOutput
    {
        get
        {
            lock (_outputLock)
            {
                return _output.ToString().Trim();
            }
        }
    }

    public Task StartAsync(
        ProcessStartInfo startInfo,
        CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        ArgumentNullException.ThrowIfNull(startInfo);
        cancellationToken.ThrowIfCancellationRequested();
        if (_process is not null)
        {
            throw new InvalidOperationException("The backend process has already been started.");
        }

        var process = new Process
        {
            StartInfo = startInfo,
            EnableRaisingEvents = true,
        };
        try
        {
            if (!process.Start())
            {
                throw new InvalidOperationException(
                    $"Could not start executable '{startInfo.FileName}'."
                );
            }
            _process = process;
            _standardOutputPump = PumpOutputAsync(process.StandardOutput);
            _standardErrorPump = PumpOutputAsync(process.StandardError);
            return Task.CompletedTask;
        }
        catch
        {
            process.Dispose();
            throw;
        }
    }

    public async Task StopAsync(TimeSpan timeout)
    {
        if (timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(timeout));
        }
        var process = _process;
        if (process is null)
        {
            return;
        }

        await ProcessTermination.KillTreeAndWaitAsync(process, timeout);
        try
        {
            await Task.WhenAll(_standardOutputPump, _standardErrorPump).WaitAsync(timeout);
        }
        catch (Exception exception) when (
            exception is TimeoutException or IOException or ObjectDisposedException
        )
        {
            // A terminated interpreter may close redirected streams asynchronously.
        }
    }

    private async Task PumpOutputAsync(StreamReader reader)
    {
        try
        {
            while (await reader.ReadLineAsync() is string line)
            {
                AppendOutput(line);
            }
        }
        catch (Exception exception) when (
            exception is IOException or ObjectDisposedException or InvalidOperationException
        )
        {
            // Process termination closes the stream; captured output remains available.
        }
    }

    private void AppendOutput(string line)
    {
        lock (_outputLock)
        {
            _output.AppendLine(line);
            if (_output.Length > MaximumCapturedCharacters)
            {
                _output.Remove(0, _output.Length - MaximumCapturedCharacters);
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        await StopAsync(TimeSpan.FromSeconds(2));
        _process?.Dispose();
        _process = null;
        _disposed = true;
    }
}

internal static class ProcessTermination
{
    public static async Task KillTreeAndWaitAsync(Process? process, TimeSpan timeout)
    {
        if (process is null || timeout <= TimeSpan.Zero)
        {
            return;
        }

        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception exception) when (
            exception is InvalidOperationException or Win32Exception or NotSupportedException
        )
        {
            return;
        }

        try
        {
            using var waitTimeout = new CancellationTokenSource(timeout);
            await process.WaitForExitAsync(waitTimeout.Token);
        }
        catch (Exception exception) when (
            exception is InvalidOperationException or ObjectDisposedException or
                OperationCanceledException
        )
        {
            // Termination is best effort after the process-tree kill request.
        }
    }
}
