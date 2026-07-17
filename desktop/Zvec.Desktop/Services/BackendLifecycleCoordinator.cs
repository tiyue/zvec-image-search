namespace Zvec.Desktop.Services;

internal sealed class BackendLifecycleCoordinator : IDisposable
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly CancellationTokenSource _shutdown = new();
    private bool _disposed;

    public bool IsShutdownRequested => _shutdown.IsCancellationRequested;

    public async Task<T> RunStartupAsync<T>(
        Func<CancellationToken, Task<T>> operation)
    {
        ArgumentNullException.ThrowIfNull(operation);
        ObjectDisposedException.ThrowIf(_disposed, this);

        var entered = false;
        try
        {
            await _gate.WaitAsync(_shutdown.Token);
            entered = true;
            _shutdown.Token.ThrowIfCancellationRequested();
            return await operation(_shutdown.Token);
        }
        finally
        {
            if (entered)
            {
                _gate.Release();
            }
        }
    }

    public async Task RunExclusiveAsync(Func<Task> operation)
    {
        ArgumentNullException.ThrowIfNull(operation);
        ObjectDisposedException.ThrowIf(_disposed, this);

        await _gate.WaitAsync();
        try
        {
            await operation();
        }
        finally
        {
            _gate.Release();
        }
    }

    public void RequestShutdown()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        _shutdown.Cancel();
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _shutdown.Cancel();
        _shutdown.Dispose();
        _gate.Dispose();
    }
}
