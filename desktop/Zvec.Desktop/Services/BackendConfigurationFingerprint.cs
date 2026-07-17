using System.Buffers;
using System.Buffers.Binary;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

public static class BackendConfigurationFingerprint
{
    private const string RuntimePackageDirectoryName = "image_vector_service";

    private static readonly string[] RequiredRuntimeFiles =
    [
        "image_service.py",
        "zvec_logging.py",
        "requirements-lock.txt",
        "model-catalog.default.json",
        "pyproject.toml",
    ];

    public static async Task<string> ComputeAsync(
        LauncherConfig config,
        string backendEntryPointPath,
        CancellationToken cancellationToken = default)
        => await ComputeAsync(
            config,
            backendEntryPointPath,
            ModelConfigurationService.CreateDefault(),
            cancellationToken
        );

    public static async Task<string> ComputeAsync(
        LauncherConfig config,
        string backendEntryPointPath,
        ModelConfigurationDocument modelConfiguration,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(config);
        ArgumentNullException.ThrowIfNull(modelConfiguration);
        var normalizedEntryPoint = BackendPathIdentity.NormalizeFilePath(
            backendEntryPointPath
        );
        var runtimePayloadHash = await ComputeRuntimePayloadHashAsync(
            normalizedEntryPoint,
            cancellationToken
        );
        return Compute(
            config,
            normalizedEntryPoint,
            runtimePayloadHash,
            modelConfiguration
        );
    }

    /// <summary>
    /// Computes a layout-independent hash of the Python files and dependency manifests
    /// that define the persistent backend. Relative paths are part of the hash so adding,
    /// removing, renaming, or changing a runtime file always changes the identity.
    /// </summary>
    internal static async Task<string> ComputeRuntimePayloadHashAsync(
        string backendEntryPointPath,
        CancellationToken cancellationToken = default)
    {
        var normalizedEntryPoint = BackendPathIdentity.NormalizeFilePath(
            backendEntryPointPath
        );
        var payloadRoot = Path.GetDirectoryName(normalizedEntryPoint);
        if (string.IsNullOrWhiteSpace(payloadRoot))
        {
            throw new InvalidDataException(
                "Backend entry point does not have a valid payload directory."
            );
        }
        payloadRoot = BackendPathIdentity.NormalizeDirectoryPath(payloadRoot);
        ValidateDirectory(payloadRoot, "Backend payload directory");

        var expectedEntryPoint = BackendPathIdentity.NormalizeFilePath(
            Path.Combine(payloadRoot, RequiredRuntimeFiles[0])
        );
        if (!PathsEqual(normalizedEntryPoint, expectedEntryPoint))
        {
            throw new InvalidDataException(
                $"Backend entry point must be '{RequiredRuntimeFiles[0]}' at the " +
                "root of the backend payload."
            );
        }

        var payloadFiles = new List<RuntimePayloadFile>();
        foreach (var relativePath in RequiredRuntimeFiles)
        {
            var fullPath = BackendPathIdentity.NormalizeFilePath(
                Path.Combine(payloadRoot, relativePath)
            );
            ValidateContainedPath(payloadRoot, fullPath);
            ValidateRegularFile(fullPath, $"Required backend runtime file '{relativePath}'");
            payloadFiles.Add(new RuntimePayloadFile(
                NormalizeRelativePath(payloadRoot, fullPath),
                fullPath
            ));
        }
        var packageDirectory = BackendPathIdentity.NormalizeDirectoryPath(
            Path.Combine(payloadRoot, RuntimePackageDirectoryName)
        );
        ValidateContainedPath(payloadRoot, packageDirectory);
        ValidateDirectory(packageDirectory, "Backend runtime package directory");
        var topLevelFileCount = payloadFiles.Count;
        CollectPythonFiles(payloadRoot, packageDirectory, payloadFiles);

        var orderedFiles = payloadFiles
            .OrderBy(file => file.RelativePath, StringComparer.Ordinal)
            .ToArray();
        if (orderedFiles.Length == topLevelFileCount)
        {
            throw new InvalidDataException(
                $"Backend runtime package '{RuntimePackageDirectoryName}' does not " +
                "contain any Python source files."
            );
        }
        var duplicatePath = orderedFiles
            .GroupBy(file => file.RelativePath, StringComparer.Ordinal)
            .FirstOrDefault(group => group.Count() > 1);
        if (duplicatePath is not null)
        {
            throw new InvalidDataException(
                $"Backend payload contains duplicate normalized path " +
                $"'{duplicatePath.Key}'."
            );
        }

        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        hash.AppendData("zvec-backend-runtime-payload-v1\0"u8);
        AppendUInt32(hash, checked((uint)orderedFiles.Length));
        foreach (var payloadFile in orderedFiles)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var relativePathBytes = Encoding.UTF8.GetBytes(payloadFile.RelativePath);
            AppendUInt32(hash, checked((uint)relativePathBytes.Length));
            hash.AppendData(relativePathBytes);
            await AppendFileContentAsync(hash, payloadFile, cancellationToken);
        }
        return ToLowerHex(hash.GetHashAndReset());
    }

    /// <summary>
    /// Deterministic overload intended for tests and callers that already hashed the
    /// backend runtime payload. Disabled libraries are intentionally excluded because
    /// they are not part of the effective backend runtime configuration.
    /// </summary>
    public static string Compute(
        LauncherConfig config,
        string backendEntryPointPath,
        string backendRuntimePayloadSha256) => Compute(
            config,
            backendEntryPointPath,
            backendRuntimePayloadSha256,
            ModelConfigurationService.CreateDefault()
        );

    public static string Compute(
        LauncherConfig config,
        string backendEntryPointPath,
        string backendRuntimePayloadSha256,
        ModelConfigurationDocument modelConfiguration)
    {
        ArgumentNullException.ThrowIfNull(config);
        ArgumentNullException.ThrowIfNull(modelConfiguration);
        ModelConfigurationService.Validate(modelConfiguration);
        if (config.SchemaVersion != LauncherConfig.CurrentSchemaVersion)
        {
            throw new InvalidDataException(
                $"Unsupported launcher configuration schema: {config.SchemaVersion}."
            );
        }
        if (!IsLowerSha256(backendRuntimePayloadSha256))
        {
            throw new ArgumentException(
                "Backend runtime payload hash must be a lowercase SHA-256 value.",
                nameof(backendRuntimePayloadSha256)
            );
        }

        var defaultLibraryId = NormalizeIdentifier(
            config.DefaultLibraryId,
            nameof(config.DefaultLibraryId)
        );
        var libraries = config.EnabledLibraries
            .Select(library => new CanonicalLibrary(
                NormalizeIdentifier(library.Id, "library id"),
                NormalizeText(library.Name, "library name"),
                BackendPathIdentity.NormalizeDirectoryForIdentity(library.ImageRoot),
                BackendPathIdentity.NormalizeDirectoryForIdentity(
                    library.WorkspaceDirectory
                )
            ))
            .OrderBy(library => library.Id, StringComparer.Ordinal)
            .ToArray();
        if (libraries.Length == 0)
        {
            throw new InvalidDataException(
                "At least one enabled library is required to fingerprint the backend."
            );
        }
        if (libraries.Select(library => library.Id).Distinct(StringComparer.Ordinal).Count() !=
            libraries.Length)
        {
            throw new InvalidDataException(
                "Enabled library identifiers must be unique, ignoring case."
            );
        }
        if (!libraries.Any(library => library.Id == defaultLibraryId))
        {
            throw new InvalidDataException(
                "The default library must be enabled before starting the backend."
            );
        }

        var buffer = new ArrayBufferWriter<byte>();
        using (var writer = new Utf8JsonWriter(buffer))
        {
            writer.WriteStartObject();
            writer.WriteNumber("schema_version", config.SchemaVersion);
            writer.WriteString("default_library_id", defaultLibraryId);
            writer.WriteString(
                "results_directory",
                BackendPathIdentity.NormalizeDirectoryForIdentity(
                    config.ResultsDirectory
                )
            );
            writer.WriteStartObject("backend_runtime_payload");
            writer.WriteString(
                "entry_point_path",
                BackendPathIdentity.NormalizeFileForIdentity(backendEntryPointPath)
            );
            writer.WriteString("sha256", backendRuntimePayloadSha256);
            writer.WriteEndObject();
            WriteCanonicalModelConfiguration(writer, modelConfiguration);
            writer.WriteStartArray("libraries");
            foreach (var library in libraries)
            {
                writer.WriteStartObject();
                writer.WriteString("id", library.Id);
                writer.WriteString("name", library.Name);
                writer.WriteString("image_root", library.ImageRoot);
                writer.WriteString("workspace_directory", library.WorkspaceDirectory);
                writer.WriteEndObject();
            }
            writer.WriteEndArray();
            writer.WriteEndObject();
        }

        return ToLowerHex(SHA256.HashData(buffer.WrittenSpan));
    }

    private static void WriteCanonicalModelConfiguration(
        Utf8JsonWriter writer,
        ModelConfigurationDocument configuration)
    {
        writer.WriteStartObject("models_configuration");
        writer.WriteNumber("schema_version", configuration.SchemaVersion);
        writer.WriteString("provider", configuration.Provider);
        writer.WriteStartArray("models");
        foreach (var model in configuration.Models!
            .OrderBy(model => model.Id, StringComparer.Ordinal))
        {
            writer.WriteStartObject();
            writer.WriteString("id", model.Id);
            writer.WriteString(
                "display_name",
                model.DisplayName!.Normalize(NormalizationForm.FormC)
            );
            writer.WriteStartArray("roles");
            foreach (var role in model.Roles!.OrderBy(role => role, StringComparer.Ordinal))
            {
                writer.WriteStringValue(role);
            }
            writer.WriteEndArray();
            writer.WriteString("protocol", model.Protocol);
            if (model.Dimension.HasValue)
            {
                writer.WriteNumber("dimension", model.Dimension.Value);
            }
            writer.WriteBoolean("enabled", model.Enabled!.Value);
            if (model.Pricing is not null)
            {
                writer.WriteStartObject("pricing");
                WriteCanonicalDecimal(
                    writer,
                    "input_yuan_per_million",
                    model.Pricing.InputYuanPerMillion!.Value
                );
                WriteCanonicalDecimal(
                    writer,
                    "output_yuan_per_million",
                    model.Pricing.OutputYuanPerMillion!.Value
                );
                writer.WriteString("effective_from", model.Pricing.EffectiveFrom);
                writer.WriteEndObject();
            }
            writer.WriteEndObject();
        }
        writer.WriteEndArray();
        writer.WriteStartObject("roles");
        writer.WriteString("embedding", configuration.Roles!.Embedding);
        writer.WriteString("auto_tag_primary", configuration.Roles.AutoTagPrimary);
        writer.WriteString(
            "auto_tag_escalation",
            configuration.Roles.AutoTagEscalation
        );
        writer.WriteEndObject();
        writer.WriteEndObject();
    }

    private static void WriteCanonicalDecimal(
        Utf8JsonWriter writer,
        string propertyName,
        decimal value)
    {
        writer.WritePropertyName(propertyName);
        writer.WriteRawValue(
            value.ToString("G29", CultureInfo.InvariantCulture),
            skipInputValidation: true
        );
    }

    public static string ComputeModelConfigurationHash(
        ModelConfigurationDocument configuration)
    {
        ArgumentNullException.ThrowIfNull(configuration);
        ModelConfigurationService.Validate(configuration);
        var buffer = new ArrayBufferWriter<byte>();
        using (var writer = new Utf8JsonWriter(buffer))
        {
            writer.WriteStartObject();
            WriteCanonicalModelConfiguration(writer, configuration);
            writer.WriteEndObject();
        }
        return ToLowerHex(SHA256.HashData(buffer.WrittenSpan));
    }

    internal static bool IsLowerSha256(string? value)
    {
        if (value is null || value.Length != 64)
        {
            return false;
        }
        foreach (var character in value)
        {
            if (character is not (>= '0' and <= '9') and not (>= 'a' and <= 'f'))
            {
                return false;
            }
        }
        return true;
    }

    private static void CollectPythonFiles(
        string payloadRoot,
        string packageDirectory,
        ICollection<RuntimePayloadFile> destination)
    {
        var pendingDirectories = new Stack<string>();
        pendingDirectories.Push(packageDirectory);
        while (pendingDirectories.Count > 0)
        {
            var directory = pendingDirectories.Pop();
            ValidateContainedPath(payloadRoot, directory);
            ValidateDirectory(directory, "Backend runtime package directory");

            foreach (var entry in new DirectoryInfo(directory).EnumerateFileSystemInfos())
            {
                var fullPath = Path.GetFullPath(entry.FullName).Normalize(
                    NormalizationForm.FormC
                );
                ValidateContainedPath(payloadRoot, fullPath);
                var attributes = GetAttributes(fullPath, "Backend runtime package entry");
                RejectReparsePoint(fullPath, attributes);
                if ((attributes & FileAttributes.Directory) != 0)
                {
                    pendingDirectories.Push(fullPath);
                    continue;
                }
                if (!string.Equals(
                    Path.GetExtension(fullPath),
                    ".py",
                    OperatingSystem.IsWindows()
                        ? StringComparison.OrdinalIgnoreCase
                        : StringComparison.Ordinal
                ))
                {
                    continue;
                }
                destination.Add(new RuntimePayloadFile(
                    NormalizeRelativePath(payloadRoot, fullPath),
                    fullPath
                ));
            }
        }
    }

    private static async Task AppendFileContentAsync(
        IncrementalHash hash,
        RuntimePayloadFile payloadFile,
        CancellationToken cancellationToken)
    {
        ValidateRegularFile(payloadFile.FullPath, $"Backend runtime file '{payloadFile.RelativePath}'");
        var lastWriteUtc = File.GetLastWriteTimeUtc(payloadFile.FullPath);
        await using var stream = new FileStream(
            payloadFile.FullPath,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            bufferSize: 64 * 1024,
            FileOptions.Asynchronous | FileOptions.SequentialScan
        );
        var expectedLength = stream.Length;
        AppendUInt64(hash, checked((ulong)expectedLength));

        var buffer = ArrayPool<byte>.Shared.Rent(64 * 1024);
        try
        {
            long remaining = expectedLength;
            while (remaining > 0)
            {
                var read = await stream.ReadAsync(
                    buffer.AsMemory(0, (int)Math.Min(buffer.Length, remaining)),
                    cancellationToken
                );
                if (read == 0)
                {
                    throw new IOException(
                        $"Backend runtime file changed while it was being hashed: " +
                        $"'{payloadFile.FullPath}'."
                    );
                }
                hash.AppendData(buffer, 0, read);
                remaining -= read;
            }
            if (await stream.ReadAsync(buffer.AsMemory(0, 1), cancellationToken) != 0)
            {
                throw new IOException(
                    $"Backend runtime file changed while it was being hashed: " +
                    $"'{payloadFile.FullPath}'."
                );
            }
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(buffer, clearArray: true);
        }

        ValidateRegularFile(payloadFile.FullPath, $"Backend runtime file '{payloadFile.RelativePath}'");
        if (File.GetLastWriteTimeUtc(payloadFile.FullPath) != lastWriteUtc)
        {
            throw new IOException(
                $"Backend runtime file changed while it was being hashed: " +
                $"'{payloadFile.FullPath}'."
            );
        }
    }

    private static void ValidateDirectory(string path, string description)
    {
        if (!PathEntryExists(path, description))
        {
            throw new DirectoryNotFoundException($"{description} was not found: '{path}'.");
        }
        var attributes = GetAttributes(path, description);
        RejectReparsePoint(path, attributes);
        if ((attributes & FileAttributes.Directory) == 0)
        {
            throw new InvalidDataException($"{description} is not a directory: '{path}'.");
        }
    }

    private static void ValidateRegularFile(string path, string description)
    {
        if (!PathEntryExists(path, description))
        {
            throw new FileNotFoundException($"{description} was not found.", path);
        }
        var attributes = GetAttributes(path, description);
        RejectReparsePoint(path, attributes);
        if ((attributes & FileAttributes.Directory) != 0)
        {
            throw new InvalidDataException($"{description} is not a regular file: '{path}'.");
        }
    }

    private static bool PathEntryExists(string path, string description)
    {
        try
        {
            var file = new FileInfo(path);
            file.Refresh();
            if (file.Exists || file.LinkTarget is not null)
            {
                return true;
            }
            var directory = new DirectoryInfo(path);
            directory.Refresh();
            return directory.Exists || directory.LinkTarget is not null;
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or NotSupportedException
        )
        {
            throw new InvalidDataException(
                $"{description} cannot be safely inspected: '{path}'.",
                exception
            );
        }
    }

    private static FileAttributes GetAttributes(string path, string description)
    {
        try
        {
            return File.GetAttributes(path);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or NotSupportedException
        )
        {
            throw new InvalidDataException(
                $"{description} cannot be safely inspected: '{path}'.",
                exception
            );
        }
    }

    private static void RejectReparsePoint(string path, FileAttributes attributes)
    {
        if ((attributes & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidDataException(
                $"Backend runtime payload cannot contain symbolic links or reparse " +
                $"points: '{path}'."
            );
        }
    }

    private static void ValidateContainedPath(string payloadRoot, string candidatePath)
    {
        var relativePath = Path.GetRelativePath(payloadRoot, candidatePath);
        if (Path.IsPathRooted(relativePath) ||
            string.Equals(relativePath, "..", StringComparison.Ordinal) ||
            relativePath.StartsWith(
                ".." + Path.DirectorySeparatorChar,
                StringComparison.Ordinal
            ) ||
            relativePath.StartsWith(
                ".." + Path.AltDirectorySeparatorChar,
                StringComparison.Ordinal
            ))
        {
            throw new InvalidDataException(
                $"Backend runtime path escapes its payload directory: '{candidatePath}'."
            );
        }
    }

    private static string NormalizeRelativePath(string payloadRoot, string fullPath)
    {
        ValidateContainedPath(payloadRoot, fullPath);
        var relativePath = Path.GetRelativePath(payloadRoot, fullPath);
        var segments = relativePath.Split(
            [Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar],
            StringSplitOptions.RemoveEmptyEntries
        );
        if (segments.Length == 0 || segments.Any(segment =>
            segment is "." or ".." || segment.IndexOf('\0') >= 0
        ))
        {
            throw new InvalidDataException(
                $"Backend runtime path is not a safe relative path: '{relativePath}'."
            );
        }
        return string.Join(
            '/',
            segments.Select(segment => segment.Normalize(NormalizationForm.FormC))
        );
    }

    private static bool PathsEqual(string left, string right) => string.Equals(
        left,
        right,
        OperatingSystem.IsWindows()
            ? StringComparison.OrdinalIgnoreCase
            : StringComparison.Ordinal
    );

    private static void AppendUInt32(IncrementalHash hash, uint value)
    {
        Span<byte> bytes = stackalloc byte[sizeof(uint)];
        BinaryPrimitives.WriteUInt32BigEndian(bytes, value);
        hash.AppendData(bytes);
    }

    private static void AppendUInt64(IncrementalHash hash, ulong value)
    {
        Span<byte> bytes = stackalloc byte[sizeof(ulong)];
        BinaryPrimitives.WriteUInt64BigEndian(bytes, value);
        hash.AppendData(bytes);
    }

    private static string NormalizeIdentifier(string? value, string fieldName) =>
        NormalizeText(value, fieldName).ToLowerInvariant();

    private static string NormalizeText(string? value, string fieldName)
    {
        var normalized = value?.Trim().Normalize(NormalizationForm.FormC) ?? string.Empty;
        if (normalized.Length == 0)
        {
            throw new InvalidDataException($"{fieldName} cannot be empty.");
        }
        return normalized;
    }

    private static string ToLowerHex(ReadOnlySpan<byte> bytes) =>
        Convert.ToHexString(bytes).ToLowerInvariant();

    private sealed record RuntimePayloadFile(string RelativePath, string FullPath);

    private sealed record CanonicalLibrary(
        string Id,
        string Name,
        string ImageRoot,
        string WorkspaceDirectory
    );
}

internal static class BackendPathIdentity
{
    public static string NormalizeDirectoryPath(string path) =>
        Path.TrimEndingDirectorySeparator(NormalizeAbsolutePath(path));

    public static string NormalizeFilePath(string path) => NormalizeAbsolutePath(path);

    public static string NormalizeDirectoryForIdentity(string path) =>
        NormalizeForIdentity(NormalizeDirectoryPath(path));

    public static string NormalizeFileForIdentity(string path) =>
        NormalizeForIdentity(NormalizeFilePath(path));

    private static string NormalizeAbsolutePath(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);
        var expanded = Environment.ExpandEnvironmentVariables(path.Trim());
        return Path.GetFullPath(expanded).Normalize(NormalizationForm.FormC);
    }

    private static string NormalizeForIdentity(string path) => OperatingSystem.IsWindows()
        ? path.ToUpperInvariant()
        : path;
}
