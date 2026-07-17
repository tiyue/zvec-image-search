using System.Net;
using System.Text.Json;
using System.Text.Json.Serialization;
using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

public sealed class BackendInstanceRegistry
{
    private const int MaximumDescriptorBytes = 64 * 1024;
    private static readonly string[] RequiredJsonProperties =
    [
        "instance_id",
        "host",
        "port",
        "process_start_utc",
        "config_fingerprint",
        "query_root",
        "runtime_config_path",
        "state",
        "generated_utc",
    ];
    private static readonly HashSet<string> KnownJsonProperties = new(
        RequiredJsonProperties.Concat(["wrapper_pid", "server_pid"]),
        StringComparer.Ordinal
    );
    private static readonly JsonSerializerOptions JsonOptions = new(
        JsonSerializerDefaults.Web
    )
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        PropertyNameCaseInsensitive = false,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        WriteIndented = true,
    };

    private readonly TimeSpan _lockRetryInterval;

    public BackendInstanceRegistry(
        string configHome,
        TimeSpan? lockRetryInterval = null)
    {
        ConfigHome = BackendPathIdentity.NormalizeDirectoryPath(configHome);
        BackendDirectory = Path.Combine(ConfigHome, "backend");
        InstancePath = Path.Combine(BackendDirectory, "instance.json");
        LaunchLockPath = Path.Combine(BackendDirectory, "launch.lock");
        _lockRetryInterval = lockRetryInterval ?? TimeSpan.FromMilliseconds(100);
        if (_lockRetryInterval < TimeSpan.FromMilliseconds(10) ||
            _lockRetryInterval > TimeSpan.FromSeconds(5))
        {
            throw new ArgumentOutOfRangeException(
                nameof(lockRetryInterval),
                "Lock retry interval must be between 10 milliseconds and 5 seconds."
            );
        }
    }

    public BackendInstanceRegistry(
        LauncherConfigService configService,
        TimeSpan? lockRetryInterval = null)
        : this(
            (configService ?? throw new ArgumentNullException(nameof(configService))).ConfigHome,
            lockRetryInterval
        )
    {
    }

    public string ConfigHome { get; }

    public string BackendDirectory { get; }

    public string InstancePath { get; }

    public string LaunchLockPath { get; }

    public async Task<BackendLaunchLock> AcquireLaunchLockAsync(
        CancellationToken cancellationToken = default)
    {
        Directory.CreateDirectory(BackendDirectory);
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                var stream = new FileStream(
                    LaunchLockPath,
                    FileMode.OpenOrCreate,
                    FileAccess.ReadWrite,
                    FileShare.None,
                    bufferSize: 1,
                    FileOptions.WriteThrough
                );
                return new BackendLaunchLock(this, stream);
            }
            catch (IOException)
            {
                await Task.Delay(_lockRetryInterval, cancellationToken);
            }
        }
    }

    public async Task<BackendInstanceDescriptor?> ReadAsync(
        CancellationToken cancellationToken = default)
    {
        if (!File.Exists(InstancePath))
        {
            return null;
        }

        try
        {
            await using var stream = new FileStream(
                InstancePath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete,
                bufferSize: 16 * 1024,
                FileOptions.Asynchronous | FileOptions.SequentialScan
            );
            if (stream.Length <= 0 || stream.Length > MaximumDescriptorBytes)
            {
                throw new InvalidDataException(
                    $"Backend instance descriptor has an invalid size: {stream.Length}."
                );
            }
            var bytes = new byte[checked((int)stream.Length)];
            await stream.ReadExactlyAsync(bytes, cancellationToken);
            ValidateJsonShape(bytes);
            var descriptor = JsonSerializer.Deserialize<BackendInstanceDescriptor>(
                bytes,
                JsonOptions
            ) ?? throw new InvalidDataException(
                "Backend instance descriptor cannot be JSON null."
            );
            ValidateDescriptor(descriptor);
            return descriptor;
        }
        catch (FileNotFoundException)
        {
            // An atomic replacement may remove the old directory entry between the
            // existence check and open. The next registry poll will observe the new one.
            return null;
        }
        catch (DirectoryNotFoundException)
        {
            return null;
        }
    }

    public async Task WriteAsync(
        BackendInstanceDescriptor descriptor,
        CancellationToken cancellationToken = default)
    {
        await using var launchLock = await AcquireLaunchLockAsync(cancellationToken);
        await WriteUnderLockAsync(descriptor, launchLock, cancellationToken);
    }

    public async Task WriteUnderLockAsync(
        BackendInstanceDescriptor descriptor,
        BackendLaunchLock launchLock,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(descriptor);
        ValidateLock(launchLock);
        ValidateDescriptor(descriptor);
        Directory.CreateDirectory(BackendDirectory);

        var bytes = JsonSerializer.SerializeToUtf8Bytes(descriptor, JsonOptions);
        if (bytes.Length > MaximumDescriptorBytes)
        {
            throw new InvalidDataException(
                "Backend instance descriptor exceeds its size limit."
            );
        }
        var temporaryPath = Path.Combine(
            BackendDirectory,
            $".instance.{Guid.NewGuid():N}.tmp"
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
                await stream.WriteAsync(bytes, cancellationToken);
                await stream.FlushAsync(cancellationToken);
                stream.Flush(flushToDisk: true);
            }
            cancellationToken.ThrowIfCancellationRequested();
            File.Move(temporaryPath, InstancePath, overwrite: true);
        }
        finally
        {
            TryDelete(temporaryPath);
        }
    }

    public async Task<bool> DeleteIfMatchesAsync(
        string instanceId,
        CancellationToken cancellationToken = default)
    {
        await using var launchLock = await AcquireLaunchLockAsync(cancellationToken);
        return await DeleteIfMatchesUnderLockAsync(
            instanceId,
            processStartUtc: null,
            launchLock,
            cancellationToken
        );
    }

    public async Task<bool> DeleteIfMatchesAsync(
        string instanceId,
        DateTimeOffset processStartUtc,
        CancellationToken cancellationToken = default)
    {
        await using var launchLock = await AcquireLaunchLockAsync(cancellationToken);
        return await DeleteIfMatchesUnderLockAsync(
            instanceId,
            processStartUtc,
            launchLock,
            cancellationToken
        );
    }

    public Task<bool> DeleteIfMatchesUnderLockAsync(
        string instanceId,
        BackendLaunchLock launchLock,
        CancellationToken cancellationToken = default) =>
        DeleteIfMatchesUnderLockAsync(
            instanceId,
            processStartUtc: null,
            launchLock,
            cancellationToken
        );

    public async Task<bool> DeleteIfMatchesUnderLockAsync(
        string instanceId,
        DateTimeOffset? processStartUtc,
        BackendLaunchLock launchLock,
        CancellationToken cancellationToken = default)
    {
        ValidateInstanceId(instanceId);
        ValidateLock(launchLock);
        if (processStartUtc is { } expectedStartUtc)
        {
            ValidateUtc(expectedStartUtc, nameof(processStartUtc));
        }
        var current = await ReadAsync(cancellationToken);
        if (current is null ||
            !string.Equals(current.InstanceId, instanceId, StringComparison.Ordinal) ||
            processStartUtc is { } expectedStart &&
            current.ProcessStartUtc != expectedStart)
        {
            return false;
        }

        cancellationToken.ThrowIfCancellationRequested();
        File.Delete(InstancePath);
        return true;
    }

    public static void ValidateDescriptor(BackendInstanceDescriptor descriptor)
    {
        ArgumentNullException.ThrowIfNull(descriptor);
        ValidateInstanceId(descriptor.InstanceId);
        if (!IPAddress.TryParse(descriptor.Host, out var host) || !IPAddress.IsLoopback(host))
        {
            throw new InvalidDataException(
                "Backend instance host must be a numeric loopback address."
            );
        }
        if (descriptor.Port is < 1 or > IPEndPoint.MaxPort)
        {
            throw new InvalidDataException("Backend instance port is outside the valid range.");
        }
        ValidateOptionalPid(descriptor.WrapperPid, "wrapper_pid");
        ValidateOptionalPid(descriptor.ServerPid, "server_pid");
        ValidateUtc(descriptor.ProcessStartUtc, "process_start_utc");
        ValidateUtc(descriptor.GeneratedUtc, "generated_utc");
        if (descriptor.GeneratedUtc < descriptor.ProcessStartUtc)
        {
            throw new InvalidDataException(
                "generated_utc cannot precede process_start_utc."
            );
        }
        if (!BackendConfigurationFingerprint.IsLowerSha256(
            descriptor.ConfigFingerprint
        ))
        {
            throw new InvalidDataException(
                "config_fingerprint must be a lowercase SHA-256 value."
            );
        }
        ValidateCanonicalPath(descriptor.QueryRoot, isDirectory: true, "query_root");
        ValidateCanonicalPath(
            descriptor.RuntimeConfigPath,
            isDirectory: false,
            "runtime_config_path"
        );
        if (!BackendInstanceStates.IsValid(descriptor.State))
        {
            throw new InvalidDataException(
                "Backend instance state must be 'starting' or 'ready'."
            );
        }
    }

    private static void ValidateJsonShape(byte[] bytes)
    {
        using var document = JsonDocument.Parse(bytes);
        if (document.RootElement.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException(
                "Backend instance descriptor must be a JSON object."
            );
        }

        var present = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in document.RootElement.EnumerateObject())
        {
            if (!KnownJsonProperties.Contains(property.Name))
            {
                throw new InvalidDataException(
                    $"Unknown backend instance property: {property.Name}."
                );
            }
            if (!present.Add(property.Name))
            {
                throw new InvalidDataException(
                    $"Duplicate backend instance property: {property.Name}."
                );
            }
        }
        var missing = RequiredJsonProperties.Where(property => !present.Contains(property));
        var missingList = string.Join(", ", missing);
        if (missingList.Length > 0)
        {
            throw new InvalidDataException(
                $"Backend instance descriptor is missing: {missingList}."
            );
        }
    }

    private static void ValidateInstanceId(string? instanceId)
    {
        if (string.IsNullOrWhiteSpace(instanceId) || instanceId.Length > 200)
        {
            throw new InvalidDataException("Backend instance_id is empty or too long.");
        }
        foreach (var character in instanceId)
        {
            if (!char.IsAsciiLetterOrDigit(character) && character is not '-' and not '_' and not '.')
            {
                throw new InvalidDataException(
                    "Backend instance_id contains an unsupported character."
                );
            }
        }
    }

    private static void ValidateOptionalPid(int? pid, string fieldName)
    {
        if (pid is <= 0)
        {
            throw new InvalidDataException($"{fieldName} must be positive when present.");
        }
    }

    private static void ValidateUtc(DateTimeOffset value, string fieldName)
    {
        if (value == default || value.Offset != TimeSpan.Zero)
        {
            throw new InvalidDataException($"{fieldName} must be a non-default UTC timestamp.");
        }
    }

    private static void ValidateCanonicalPath(
        string path,
        bool isDirectory,
        string fieldName)
    {
        if (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path))
        {
            throw new InvalidDataException($"{fieldName} must be an absolute path.");
        }
        string normalized;
        try
        {
            normalized = isDirectory
                ? BackendPathIdentity.NormalizeDirectoryPath(path)
                : BackendPathIdentity.NormalizeFilePath(path);
        }
        catch (Exception exception) when (
            exception is ArgumentException or NotSupportedException or PathTooLongException
        )
        {
            throw new InvalidDataException($"{fieldName} is not a valid path.", exception);
        }

        var comparison = OperatingSystem.IsWindows()
            ? StringComparison.OrdinalIgnoreCase
            : StringComparison.Ordinal;
        if (!string.Equals(path, normalized, comparison))
        {
            throw new InvalidDataException($"{fieldName} must be stored in normalized form.");
        }
    }

    private void ValidateLock(BackendLaunchLock launchLock)
    {
        ArgumentNullException.ThrowIfNull(launchLock);
        if (!launchLock.IsHeldBy(this))
        {
            throw new InvalidOperationException(
                "The supplied launch lock is not held by this backend registry."
            );
        }
    }

    private static void TryDelete(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (FileNotFoundException)
        {
            // Best-effort cleanup after an interrupted atomic write.
        }
        catch (DirectoryNotFoundException)
        {
            // Best-effort cleanup after an interrupted atomic write.
        }
    }
}

public sealed class BackendLaunchLock : IDisposable, IAsyncDisposable
{
    private readonly BackendInstanceRegistry _owner;
    private FileStream? _stream;

    internal BackendLaunchLock(BackendInstanceRegistry owner, FileStream stream)
    {
        _owner = owner;
        _stream = stream;
    }

    public bool IsHeld => _stream is not null;

    internal bool IsHeldBy(BackendInstanceRegistry owner) =>
        ReferenceEquals(_owner, owner) && IsHeld;

    public void Dispose()
    {
        _stream?.Dispose();
        _stream = null;
    }

    public async ValueTask DisposeAsync()
    {
        if (_stream is { } stream)
        {
            _stream = null;
            await stream.DisposeAsync();
        }
    }
}
