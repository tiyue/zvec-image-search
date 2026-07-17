using System.Text.Json;
using Zvec.Desktop.Models;
using Zvec.Desktop.Services;

var temporaryRoot = Path.Combine(
    Path.GetTempPath(),
    $"zvec-model-config-tests-{Guid.NewGuid():N}"
);
Directory.CreateDirectory(temporaryRoot);
try
{
    var path = Path.Combine(temporaryRoot, "models.json");
    var service = new ModelConfigurationService(path);

    var created = await service.LoadAsync(createIfMissing: true);
    Assert(created.IsValid && created.WasCreated, "first load creates valid defaults");
    Assert(File.Exists(path), "default models.json exists");
    var defaultJson = await File.ReadAllTextAsync(path);
    Assert(!defaultJson.Contains("api_key", StringComparison.OrdinalIgnoreCase),
        "models.json contains no API key field");
    Assert(defaultJson.Contains("input_yuan_per_million", StringComparison.Ordinal),
        "default visual models include pricing");

    var bundledDefaultPath = Path.Combine(
        AppContext.BaseDirectory,
        "backend",
        "model-catalog.default.json"
    );
    Assert(File.Exists(bundledDefaultPath), "published default model catalog exists");
    var bundledDefault = await new ModelConfigurationService(bundledDefaultPath)
        .LoadStrictAsync(createIfMissing: false);
    Assert(
        BackendConfigurationFingerprint.ComputeModelConfigurationHash(bundledDefault) ==
        BackendConfigurationFingerprint.ComputeModelConfigurationHash(
            ModelConfigurationService.CreateDefault()
        ),
        "published catalog and WPF built-in defaults remain identical"
    );

    var customModels = created.Configuration.Models!
        .Select(model => CloneModel(model))
        .ToList();
    customModels.Add(new ModelDefinition
    {
        Id = "future:vl-fast",
        DisplayName = "Future VL Fast",
        Roles = [ModelRoles.AutoTagPrimary, ModelRoles.AutoTagEscalation],
        Protocol = ModelProtocols.MultimodalConversation,
        Enabled = true,
        Pricing = new ModelPricing
        {
            InputYuanPerMillion = 0.2m,
            OutputYuanPerMillion = 2m,
            EffectiveFrom = "2026-07-16",
        },
    });
    var extensible = new ModelConfigurationDocument
    {
        SchemaVersion = 1,
        Provider = ModelConfigurationService.SupportedProvider,
        Models = customModels,
        Roles = new ModelRoleAssignments
        {
            Embedding = "qwen3-vl-embedding",
            AutoTagPrimary = "future:vl-fast",
            AutoTagEscalation = "qwen3-vl-plus",
        },
    };
    await service.SaveAsync(extensible);
    var reloaded = await service.LoadAsync();
    Assert(reloaded.IsValid, "future compatible DashScope model reloads");
    Assert(reloaded.Configuration.Roles!.AutoTagPrimary == "future:vl-fast",
        "custom role assignment is retained");
    Assert(!Directory.EnumerateFiles(temporaryRoot, "*.tmp").Any(),
        "atomic save leaves no temporary file");

    await File.WriteAllTextAsync(path,
        """
        {
          "schema_version": 1,
          "provider": "aliyun_dashscope",
          "models": [],
          "roles": {},
          "dashscope_api_key": "must-never-be-accepted"
        }
        """);
    var invalid = await service.LoadAsync();
    Assert(!invalid.IsValid && invalid.IsUsingLastValidConfiguration,
        "unknown secret field is rejected without replacing last valid config");
    Assert(invalid.Configuration.Roles!.AutoTagPrimary == "future:vl-fast",
        "invalid reload preserves last valid role assignment");
    await AssertThrowsAsync<InvalidDataException>(
        () => service.LoadStrictAsync(createIfMissing: false),
        "strict backend load rejects unknown fields"
    );

    var duplicateProviderJson = defaultJson.Replace(
        "\"provider\": \"aliyun_dashscope\"",
        "\"provider\": \"aliyun_dashscope\",\n  \"provider\": \"aliyun_dashscope\"",
        StringComparison.Ordinal
    );
    await File.WriteAllTextAsync(path, duplicateProviderJson);
    await AssertThrowsAsync<InvalidDataException>(
        () => service.LoadStrictAsync(createIfMissing: false),
        "duplicate JSON properties are rejected"
    );

    var incompatibleEmbedding = ModelConfigurationService.CreateDefault();
    incompatibleEmbedding.Models![0] = CloneModel(
        incompatibleEmbedding.Models[0],
        dimension: 768
    );
    AssertThrows<InvalidDataException>(
        () => ModelConfigurationService.Validate(incompatibleEmbedding),
        "embedding dimension mismatch is rejected"
    );

    var missingPricing = ModelConfigurationService.CreateDefault();
    missingPricing.Models![1] = CloneModel(missingPricing.Models[1], omitPricing: true);
    AssertThrows<InvalidDataException>(
        () => ModelConfigurationService.Validate(missingPricing),
        "selected visual model without pricing is rejected"
    );

    var uppercaseId = ModelConfigurationService.CreateDefault();
    uppercaseId.Models![1] = CloneModel(uppercaseId.Models[1], id: "Qwen-Custom");
    AssertThrows<InvalidDataException>(
        () => ModelConfigurationService.Validate(uppercaseId),
        "model IDs use the same lowercase grammar as Python"
    );

    var launcherConfig = new LauncherConfig
    {
        SchemaVersion = LauncherConfig.CurrentSchemaVersion,
        ResultsDirectory = Directory.CreateDirectory(Path.Combine(temporaryRoot, "results")).FullName,
        DefaultLibraryId = "main",
        Libraries =
        [
            new LauncherLibrary
            {
                Id = "main",
                Name = "Main",
                ImageRoot = Directory.CreateDirectory(Path.Combine(temporaryRoot, "images")).FullName,
                WorkspaceDirectory = Directory.CreateDirectory(
                    Path.Combine(temporaryRoot, "workspace")
                ).FullName,
                Enabled = true,
            },
        ],
    };
    var runtimeHash = new string('0', 64);
    var entryPoint = Path.Combine(temporaryRoot, "image_service.py");
    var defaultFingerprint = BackendConfigurationFingerprint.Compute(
        launcherConfig,
        entryPoint,
        runtimeHash
    );
    var explicitDefaultFingerprint = BackendConfigurationFingerprint.Compute(
        launcherConfig,
        entryPoint,
        runtimeHash,
        ModelConfigurationService.CreateDefault()
    );
    Assert(defaultFingerprint == explicitDefaultFingerprint,
        "missing models.json is fingerprint-equivalent to defaults");
    var changedRoles = ModelConfigurationService.WithRoleAssignments(
        ModelConfigurationService.CreateDefault(),
        "qwen3-vl-embedding",
        "qwen3-vl-plus",
        "qwen3-vl-plus"
    );
    Assert(defaultFingerprint != BackendConfigurationFingerprint.Compute(
        launcherConfig,
        entryPoint,
        runtimeHash,
        changedRoles
    ), "effective model role changes alter backend fingerprint");

    await TestReparsePointRejectionAsync(temporaryRoot);
    AssertThrows<InvalidDataException>(
        () => ModelConfigurationService.ValidateConfigurationFileAttributes(
            FileAttributes.ReparsePoint
        ),
        "reparse-point attributes are rejected even when host cannot create a symlink"
    );

    Console.WriteLine("Model configuration tests passed.");
}
finally
{
    try
    {
        Directory.Delete(temporaryRoot, recursive: true);
    }
    catch (IOException)
    {
        // A temp cleanup failure does not change the test assertions.
    }
    catch (UnauthorizedAccessException)
    {
        // A temp cleanup failure does not change the test assertions.
    }
}

static ModelDefinition CloneModel(
    ModelDefinition source,
    int? dimension = null,
    bool omitPricing = false,
    string? id = null) =>
    new()
    {
        Id = id ?? source.Id,
        DisplayName = source.DisplayName,
        Roles = source.Roles?.ToList(),
        Protocol = source.Protocol,
        Dimension = dimension ?? source.Dimension,
        Enabled = source.Enabled,
        Pricing = omitPricing ? null : source.Pricing,
    };

static void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException($"Assertion failed: {message}");
    }
}

static async Task AssertThrowsAsync<TException>(Func<Task> action, string message)
    where TException : Exception
{
    try
    {
        await action();
    }
    catch (TException)
    {
        return;
    }
    throw new InvalidOperationException($"Assertion failed: {message}");
}

static async Task TestReparsePointRejectionAsync(string root)
{
    var target = Path.Combine(root, "models-target.json");
    var link = Path.Combine(root, "models-link.json");
    await File.WriteAllTextAsync(target, "{}");
    try
    {
        File.CreateSymbolicLink(link, target);
    }
    catch (Exception exception) when (
        exception is UnauthorizedAccessException or PlatformNotSupportedException or IOException)
    {
        Console.WriteLine($"Reparse-point test skipped on this host: {exception.GetType().Name}.");
        return;
    }
    var service = new ModelConfigurationService(link);
    await AssertThrowsAsync<InvalidDataException>(
        () => service.LoadStrictAsync(createIfMissing: false),
        "models.json reparse point is rejected on load"
    );
    await AssertThrowsAsync<InvalidDataException>(
        () => service.SaveAsync(ModelConfigurationService.CreateDefault()),
        "models.json reparse point is rejected on save"
    );
}

static void AssertThrows<TException>(Action action, string message)
    where TException : Exception
{
    try
    {
        action();
    }
    catch (TException)
    {
        return;
    }
    throw new InvalidOperationException($"Assertion failed: {message}");
}
