using System.Security.Cryptography;
using System.Text;
using System.Runtime.Versioning;

namespace Zvec.Desktop.Services;

public interface ISecureSecretStore
{
    bool Exists();

    string? Read();

    void Write(string secret);

    void Delete();
}

public sealed class BackendSessionTokenStore
{
    private const string CredentialTargetPrefix = "Zvec.ImageSearch/BackendSession/";
    private readonly ISecureSecretStore _secretStore;

    public BackendSessionTokenStore(
        string configHome,
        ISecureSecretStore? secretStore = null)
    {
        ConfigHome = BackendPathIdentity.NormalizeDirectoryPath(configHome);
        CredentialTarget = CreateCredentialTarget(ConfigHome);
        FallbackSecretPath = Path.Combine(
            ConfigHome,
            "backend",
            "secrets",
            $"{Sha256Hex(CredentialTarget)}.secret"
        );
        _secretStore = secretStore ?? CreatePlatformStore(
            CredentialTarget,
            FallbackSecretPath
        );
    }

    public string ConfigHome { get; }

    public string CredentialTarget { get; }

    public string FallbackSecretPath { get; }

    public bool HasToken() => _secretStore.Exists();

    public string? ReadToken()
    {
        var token = _secretStore.Read();
        if (token is not null)
        {
            ValidateToken(token, nameof(token));
        }
        return token;
    }

    public void SaveToken(string token)
    {
        ValidateToken(token, nameof(token));
        _secretStore.Write(token);
    }

    public void DeleteToken() => _secretStore.Delete();

    /// <summary>
    /// Prevents an older desktop process from deleting a token that was replaced by a
    /// newer backend instance. Callers should perform this while holding launch.lock.
    /// </summary>
    public bool DeleteIfMatches(string expectedToken)
    {
        ValidateToken(expectedToken, nameof(expectedToken));
        var current = _secretStore.Read();
        if (current is null || !FixedTimeEquals(current, expectedToken))
        {
            return false;
        }
        _secretStore.Delete();
        return true;
    }

    public static string CreateCredentialTarget(string configHome)
    {
        var identity = BackendPathIdentity.NormalizeDirectoryForIdentity(configHome);
        return CredentialTargetPrefix + Sha256Hex(identity);
    }

    private static ISecureSecretStore CreatePlatformStore(
        string credentialTarget,
        string fallbackSecretPath) => OperatingSystem.IsWindows()
        ? new WindowsCredentialSecretStore(credentialTarget)
        : new UserPrivateFileSecretStore(fallbackSecretPath);

    private static void ValidateToken(string token, string parameterName)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(token, parameterName);
        if (Encoding.UTF8.GetByteCount(token) > 1024 || token.Any(char.IsControl))
        {
            throw new ArgumentException(
                "Backend session token is invalid or exceeds its size limit.",
                parameterName
            );
        }
    }

    private static bool FixedTimeEquals(string left, string right)
    {
        var leftBytes = Encoding.UTF8.GetBytes(left);
        var rightBytes = Encoding.UTF8.GetBytes(right);
        try
        {
            return leftBytes.Length == rightBytes.Length &&
                CryptographicOperations.FixedTimeEquals(leftBytes, rightBytes);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(leftBytes);
            CryptographicOperations.ZeroMemory(rightBytes);
        }
    }

    private static string Sha256Hex(string value) => Convert
        .ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value)))
        .ToLowerInvariant();
}

public sealed class WindowsCredentialSecretStore : ISecureSecretStore
{
    private readonly CredentialManagerService _credentialManager;

    public WindowsCredentialSecretStore(string targetName)
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "Windows Credential Manager is only available on Windows."
            );
        }
        ArgumentException.ThrowIfNullOrWhiteSpace(targetName);
        _credentialManager = new CredentialManagerService(targetName);
    }

    public bool Exists() => _credentialManager.HasSecret();

    public string? Read() => _credentialManager.ReadSecret();

    public void Write(string secret) => _credentialManager.SaveSecret(secret);

    public void Delete() => _credentialManager.DeleteSecret();
}

/// <summary>
/// Non-Windows fallback for native CLI-compatible builds. The WPF product itself is
/// Windows-only, but keeping this implementation user-private makes the component safe
/// to reuse from a future cross-platform host.
/// </summary>
[UnsupportedOSPlatform("windows")]
public sealed class UserPrivateFileSecretStore : ISecureSecretStore
{
    private const int MaximumSecretBytes = 4096;
    private static readonly UTF8Encoding StrictUtf8 = new(
        encoderShouldEmitUTF8Identifier: false,
        throwOnInvalidBytes: true
    );
    private readonly string _path;

    public UserPrivateFileSecretStore(string path)
    {
        if (OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "The private-file secret fallback is only supported on POSIX systems."
            );
        }
        _path = BackendPathIdentity.NormalizeFilePath(path);
    }

    public bool Exists() => File.Exists(_path);

    public string? Read()
    {
        if (!File.Exists(_path))
        {
            return null;
        }
        RejectSymbolicLink();
        EnsurePrivateFileMode();
        var length = new FileInfo(_path).Length;
        if (length <= 0 || length > MaximumSecretBytes)
        {
            throw new InvalidDataException("Stored secret has an invalid length.");
        }
        return StrictUtf8.GetString(File.ReadAllBytes(_path));
    }

    public void Write(string secret)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(secret);
        var bytes = StrictUtf8.GetBytes(secret);
        if (bytes.Length > MaximumSecretBytes)
        {
            CryptographicOperations.ZeroMemory(bytes);
            throw new ArgumentException(
                "Secret exceeds the private-file size limit.",
                nameof(secret)
            );
        }

        var directory = Path.GetDirectoryName(_path) ?? throw new InvalidOperationException(
            "Secret path must have a parent directory."
        );
        Directory.CreateDirectory(directory);
        File.SetUnixFileMode(
            directory,
            UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute
        );
        var temporaryPath = Path.Combine(
            directory,
            $".{Path.GetFileName(_path)}.{Guid.NewGuid():N}.tmp"
        );
        try
        {
            var options = new FileStreamOptions
            {
                Mode = FileMode.CreateNew,
                Access = FileAccess.Write,
                Share = FileShare.None,
                Options = FileOptions.WriteThrough,
                UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite,
            };
            using (var stream = new FileStream(temporaryPath, options))
            {
                stream.Write(bytes);
                stream.Flush(flushToDisk: true);
            }
            File.Move(temporaryPath, _path, overwrite: true);
            File.SetUnixFileMode(
                _path,
                UnixFileMode.UserRead | UnixFileMode.UserWrite
            );
        }
        finally
        {
            CryptographicOperations.ZeroMemory(bytes);
            TryDelete(temporaryPath);
        }
    }

    public void Delete() => TryDelete(_path);

    private void RejectSymbolicLink()
    {
        if (new FileInfo(_path).LinkTarget is not null)
        {
            throw new InvalidDataException("Refusing to read a symbolic-link secret file.");
        }
    }

    private void EnsurePrivateFileMode()
    {
        var mode = File.GetUnixFileMode(_path);
        const UnixFileMode forbidden =
            UnixFileMode.GroupRead |
            UnixFileMode.GroupWrite |
            UnixFileMode.GroupExecute |
            UnixFileMode.OtherRead |
            UnixFileMode.OtherWrite |
            UnixFileMode.OtherExecute;
        if ((mode & forbidden) != 0)
        {
            throw new UnauthorizedAccessException(
                "Stored secret is accessible by another POSIX user or group."
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
            // Idempotent deletion is required during crash recovery.
        }
        catch (DirectoryNotFoundException)
        {
            // Idempotent deletion is required during crash recovery.
        }
    }
}
