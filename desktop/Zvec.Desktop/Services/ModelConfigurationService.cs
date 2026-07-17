using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using System.Globalization;
using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

public sealed partial class ModelConfigurationService
{
    public const int CurrentSchemaVersion = 1;
    public const string SupportedProvider = "aliyun_dashscope";
    public const int RequiredEmbeddingDimension = 1024;

    private const int MaximumConfigurationBytes = 1024 * 1024;
    private const int MaximumModels = 100;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private ModelConfigurationDocument _lastValidConfiguration = CreateDefault();

    private static readonly JsonSerializerOptions ReadOptions = new()
    {
        AllowTrailingCommas = false,
        PropertyNameCaseInsensitive = false,
        ReadCommentHandling = JsonCommentHandling.Disallow,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
    };

    private static readonly JsonSerializerOptions WriteOptions = new()
    {
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        WriteIndented = true,
    };

    public ModelConfigurationService(string? configurationPath = null)
    {
        ConfigurationPath = Path.GetFullPath(configurationPath ?? DefaultConfigurationPath);
    }

    public static string DefaultConfigurationPath
    {
        get
        {
            var explicitPath = Environment.GetEnvironmentVariable("ZVEC_MODELS_CONFIG");
            if (!string.IsNullOrWhiteSpace(explicitPath))
            {
                return Path.GetFullPath(Environment.ExpandEnvironmentVariables(
                    explicitPath.Trim()
                ));
            }
            var configHome = Environment.GetEnvironmentVariable("ZVEC_CONFIG_HOME");
            if (!string.IsNullOrWhiteSpace(configHome))
            {
                return Path.Combine(
                    Path.GetFullPath(Environment.ExpandEnvironmentVariables(configHome.Trim())),
                    "models.json"
                );
            }
            return Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "zvec-image-search",
                "models.json"
            );
        }
    }

    public string ConfigurationPath { get; }

    public async Task<ModelConfigurationLoadResult> LoadAsync(
        bool createIfMissing = true,
        CancellationToken cancellationToken = default)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            RejectReparsePointIfPresent(ConfigurationPath);
            if (!File.Exists(ConfigurationPath))
            {
                var defaults = CreateDefault();
                if (createIfMissing)
                {
                    await SaveCoreAsync(defaults, cancellationToken).ConfigureAwait(false);
                }
                _lastValidConfiguration = Clone(defaults);
                return new ModelConfigurationLoadResult(
                    Clone(defaults),
                    IsValid: true,
                    IsUsingLastValidConfiguration: false,
                    WasCreated: createIfMissing,
                    Error: null
                );
            }

            var info = new FileInfo(ConfigurationPath);
            if (info.Length > MaximumConfigurationBytes)
            {
                throw new InvalidDataException("模型配置超过 1 MB，已拒绝读取。");
            }
            var json = await File.ReadAllTextAsync(
                ConfigurationPath,
                Encoding.UTF8,
                cancellationToken
            ).ConfigureAwait(false);
            var configuration = DeserializeAndValidate(json);
            _lastValidConfiguration = Clone(configuration);
            return new ModelConfigurationLoadResult(
                Clone(configuration),
                IsValid: true,
                IsUsingLastValidConfiguration: false,
                WasCreated: false,
                Error: null
            );
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or InvalidDataException)
        {
            return new ModelConfigurationLoadResult(
                Clone(_lastValidConfiguration),
                IsValid: false,
                IsUsingLastValidConfiguration: true,
                WasCreated: false,
                Error: exception.Message
            );
        }
        finally
        {
            _gate.Release();
        }
    }

    /// <summary>
    /// Loads the exact effective document used to start a backend. Malformed JSON is
    /// deliberately propagated instead of being converted to the UI's last-valid fallback.
    /// </summary>
    public async Task<ModelConfigurationDocument> LoadStrictAsync(
        bool createIfMissing,
        CancellationToken cancellationToken = default)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            RejectReparsePointIfPresent(ConfigurationPath);
            if (!File.Exists(ConfigurationPath))
            {
                var defaults = CreateDefault();
                if (createIfMissing)
                {
                    await SaveCoreAsync(defaults, cancellationToken).ConfigureAwait(false);
                }
                _lastValidConfiguration = Clone(defaults);
                return Clone(defaults);
            }
            var info = new FileInfo(ConfigurationPath);
            if (info.Length > MaximumConfigurationBytes)
            {
                throw new InvalidDataException("模型配置超过 1 MB，已拒绝读取。");
            }
            var json = await File.ReadAllTextAsync(
                ConfigurationPath,
                Encoding.UTF8,
                cancellationToken
            ).ConfigureAwait(false);
            var configuration = DeserializeAndValidate(json);
            _lastValidConfiguration = Clone(configuration);
            return Clone(configuration);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<ModelConfigurationDocument> SaveAsync(
        ModelConfigurationDocument configuration,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(configuration);
        Validate(configuration);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await SaveCoreAsync(configuration, cancellationToken).ConfigureAwait(false);
            _lastValidConfiguration = Clone(configuration);
            return Clone(configuration);
        }
        finally
        {
            _gate.Release();
        }
    }

    public static ModelConfigurationDocument CreateDefault() => new()
    {
        SchemaVersion = CurrentSchemaVersion,
        Provider = SupportedProvider,
        Models =
        [
            new ModelDefinition
            {
                Id = "qwen3-vl-embedding",
                DisplayName = "Qwen3-VL Embedding",
                Roles = [ModelRoles.Embedding],
                Protocol = ModelProtocols.MultimodalEmbedding,
                Dimension = RequiredEmbeddingDimension,
                Enabled = true,
            },
            new ModelDefinition
            {
                Id = "qwen3-vl-flash",
                DisplayName = "Qwen3-VL Flash",
                Roles = [ModelRoles.AutoTagPrimary, ModelRoles.AutoTagEscalation],
                Protocol = ModelProtocols.MultimodalConversation,
                Enabled = true,
                Pricing = new ModelPricing
                {
                    InputYuanPerMillion = 0.15m,
                    OutputYuanPerMillion = 1.50m,
                    EffectiveFrom = "2026-07-14",
                },
            },
            new ModelDefinition
            {
                Id = "qwen3-vl-plus",
                DisplayName = "Qwen3-VL Plus",
                Roles = [ModelRoles.AutoTagPrimary, ModelRoles.AutoTagEscalation],
                Protocol = ModelProtocols.MultimodalConversation,
                Enabled = true,
                Pricing = new ModelPricing
                {
                    InputYuanPerMillion = 1.00m,
                    OutputYuanPerMillion = 10.00m,
                    EffectiveFrom = "2026-07-14",
                },
            },
        ],
        Roles = new ModelRoleAssignments
        {
            Embedding = "qwen3-vl-embedding",
            AutoTagPrimary = "qwen3-vl-flash",
            AutoTagEscalation = "qwen3-vl-plus",
        },
    };

    public static void Validate(ModelConfigurationDocument configuration)
    {
        ArgumentNullException.ThrowIfNull(configuration);
        if (configuration.SchemaVersion != CurrentSchemaVersion)
        {
            throw new InvalidDataException(
                $"不支持 schema_version={configuration.SchemaVersion}；当前只支持版本 1。"
            );
        }
        if (!string.Equals(
            configuration.Provider,
            SupportedProvider,
            StringComparison.Ordinal
        ))
        {
            throw new InvalidDataException(
                $"provider 必须是“{SupportedProvider}”；当前仅支持阿里云 DashScope。"
            );
        }
        if (configuration.Models is not { Count: > 0 })
        {
            throw new InvalidDataException("models 至少需要包含一个模型。");
        }
        if (configuration.Models.Count > MaximumModels)
        {
            throw new InvalidDataException($"models 最多允许 {MaximumModels} 个模型。");
        }
        if (configuration.Roles is null)
        {
            throw new InvalidDataException("roles 不能为空。");
        }

        var models = new Dictionary<string, ModelDefinition>(StringComparer.Ordinal);
        foreach (var model in configuration.Models)
        {
            ValidateModel(model);
            if (!models.TryAdd(model.Id!, model))
            {
                throw new InvalidDataException($"模型 ID“{model.Id}”重复。");
            }
        }

        ValidateRole(ModelRoles.Embedding, configuration.Roles.Embedding, models);
        ValidateRole(ModelRoles.AutoTagPrimary, configuration.Roles.AutoTagPrimary, models);
        ValidateRole(
            ModelRoles.AutoTagEscalation,
            configuration.Roles.AutoTagEscalation,
            models
        );
    }

    public static string DescribeCapability(ModelDefinition model)
    {
        ArgumentNullException.ThrowIfNull(model);
        return model.Protocol switch
        {
            ModelProtocols.MultimodalEmbedding =>
                $"图文向量编码 · {model.Dimension ?? 0} 维 · 索引与语义检索",
            ModelProtocols.MultimodalConversation =>
                model.Roles?.Contains(ModelRoles.AutoTagEscalation, StringComparer.Ordinal) == true
                    ? "视觉理解与标签生成 · 可用于主标注或疑难升级" +
                      DescribePricing(model.Pricing)
                    : "视觉理解与标签生成" + DescribePricing(model.Pricing),
            _ => model.Protocol ?? string.Empty,
        };
    }

    public static ModelConfigurationDocument WithRoleAssignments(
        ModelConfigurationDocument source,
        string embedding,
        string autoTagPrimary,
        string autoTagEscalation) =>
        new()
        {
            SchemaVersion = source.SchemaVersion,
            Provider = source.Provider,
            Models = CloneModels(source.Models),
            Roles = new ModelRoleAssignments
            {
                Embedding = embedding,
                AutoTagPrimary = autoTagPrimary,
                AutoTagEscalation = autoTagEscalation,
            },
        };

    private static void ValidateModel(ModelDefinition model)
    {
        if (string.IsNullOrWhiteSpace(model.Id) || !ModelIdPattern().IsMatch(model.Id))
        {
            throw new InvalidDataException(
                "每个模型都需要安全的小写 id（字母或数字开头，仅含小写字母、数字、点、下划线、冒号和短横线）。"
            );
        }
        if (string.IsNullOrWhiteSpace(model.DisplayName) || model.DisplayName.Length > 128)
        {
            throw new InvalidDataException($"模型“{model.Id}”的 display_name 无效。");
        }
        if (!model.Enabled.HasValue)
        {
            throw new InvalidDataException($"模型“{model.Id}”缺少 enabled。");
        }
        if (model.Roles is not { Count: > 0 })
        {
            throw new InvalidDataException($"模型“{model.Id}”至少需要声明一个 role。");
        }
        var roleSet = new HashSet<string>(StringComparer.Ordinal);
        foreach (var role in model.Roles)
        {
            if (!ModelRoles.All.Contains(role))
            {
                throw new InvalidDataException($"模型“{model.Id}”声明了未知角色“{role}”。");
            }
            if (!roleSet.Add(role))
            {
                throw new InvalidDataException($"模型“{model.Id}”重复声明角色“{role}”。");
            }
        }

        switch (model.Protocol)
        {
            case ModelProtocols.MultimodalEmbedding:
                if (!roleSet.SetEquals([ModelRoles.Embedding]))
                {
                    throw new InvalidDataException(
                        $"向量模型“{model.Id}”只能声明 embedding 角色。"
                    );
                }
                if (model.Dimension != RequiredEmbeddingDimension)
                {
                    throw new InvalidDataException(
                        $"向量模型“{model.Id}”的 dimension 必须是 {RequiredEmbeddingDimension}，否则与现有 Collection 不兼容。"
                    );
                }
                break;
            case ModelProtocols.MultimodalConversation:
                if (model.Dimension.HasValue || roleSet.Contains(ModelRoles.Embedding))
                {
                    throw new InvalidDataException(
                        $"视觉理解模型“{model.Id}”不能声明 dimension 或 embedding 角色。"
                    );
                }
                break;
            default:
                throw new InvalidDataException(
                    $"模型“{model.Id}”使用了不支持的 protocol“{model.Protocol}”。"
                );
        }
        if (model.Pricing is not null)
        {
            ValidatePricing(model.Id, model.Pricing);
        }
    }

    private static void ValidateRole(
        string role,
        string? modelId,
        IReadOnlyDictionary<string, ModelDefinition> models)
    {
        if (string.IsNullOrWhiteSpace(modelId))
        {
            throw new InvalidDataException($"roles.{role} 不能为空。");
        }
        if (!models.TryGetValue(modelId, out var model))
        {
            throw new InvalidDataException($"roles.{role} 引用了不存在的模型“{modelId}”。");
        }
        if (model.Enabled != true)
        {
            throw new InvalidDataException($"roles.{role} 引用了已停用模型“{modelId}”。");
        }
        if (model.Roles?.Contains(role, StringComparer.Ordinal) != true)
        {
            throw new InvalidDataException(
                $"模型“{modelId}”未声明角色“{role}”，不能用于 roles.{role}。"
            );
        }
        if (!string.Equals(role, ModelRoles.Embedding, StringComparison.Ordinal) &&
            model.Pricing is null)
        {
            throw new InvalidDataException(
                $"视觉模型“{modelId}”被选中时必须提供 pricing，才能执行预算保护。"
            );
        }
    }

    private async Task SaveCoreAsync(
        ModelConfigurationDocument configuration,
        CancellationToken cancellationToken)
    {
        Validate(configuration);
        RejectReparsePointIfPresent(ConfigurationPath);
        var directory = Path.GetDirectoryName(ConfigurationPath)
            ?? throw new InvalidOperationException("模型配置目录无效。");
        Directory.CreateDirectory(directory);
        var temporaryPath = Path.Combine(
            directory,
            $".{Path.GetFileName(ConfigurationPath)}.{Guid.NewGuid():N}.tmp"
        );
        try
        {
            var json = JsonSerializer.Serialize(configuration, WriteOptions) + Environment.NewLine;
            var bytes = Encoding.UTF8.GetBytes(json);
            await using (var stream = new FileStream(
                temporaryPath,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                16 * 1024,
                FileOptions.Asynchronous | FileOptions.WriteThrough
            ))
            {
                await stream.WriteAsync(bytes, cancellationToken).ConfigureAwait(false);
                await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
                stream.Flush(flushToDisk: true);
            }

            if (File.Exists(ConfigurationPath))
            {
                RejectReparsePointIfPresent(ConfigurationPath);
                File.Replace(temporaryPath, ConfigurationPath, null, ignoreMetadataErrors: true);
            }
            else
            {
                File.Move(temporaryPath, ConfigurationPath);
            }
        }
        finally
        {
            try
            {
                File.Delete(temporaryPath);
            }
            catch (IOException)
            {
                // A failed cleanup must not hide the original save error.
            }
            catch (UnauthorizedAccessException)
            {
                // A failed cleanup must not hide the original save error.
            }
        }
    }

    private static ModelConfigurationDocument Clone(ModelConfigurationDocument source) => new()
    {
        SchemaVersion = source.SchemaVersion,
        Provider = source.Provider,
        Models = CloneModels(source.Models),
        Roles = source.Roles is null
            ? null
            : new ModelRoleAssignments
            {
                Embedding = source.Roles.Embedding,
                AutoTagPrimary = source.Roles.AutoTagPrimary,
                AutoTagEscalation = source.Roles.AutoTagEscalation,
            },
    };

    private static List<ModelDefinition>? CloneModels(IEnumerable<ModelDefinition>? models) =>
        models?.Select(model => new ModelDefinition
        {
            Id = model.Id,
            DisplayName = model.DisplayName,
            Roles = model.Roles?.ToList(),
            Protocol = model.Protocol,
            Dimension = model.Dimension,
            Enabled = model.Enabled,
            Pricing = model.Pricing is null
                ? null
                : new ModelPricing
                {
                    InputYuanPerMillion = model.Pricing.InputYuanPerMillion,
                    OutputYuanPerMillion = model.Pricing.OutputYuanPerMillion,
                    EffectiveFrom = model.Pricing.EffectiveFrom,
                },
        }).ToList();

    private static void ValidatePricing(string modelId, ModelPricing pricing)
    {
        if (pricing.InputYuanPerMillion is null or < 0 ||
            pricing.OutputYuanPerMillion is null or < 0)
        {
            throw new InvalidDataException(
                $"模型“{modelId}”的 pricing 输入、输出单价必须是非负数。"
            );
        }
        if (string.IsNullOrWhiteSpace(pricing.EffectiveFrom) ||
            !DateOnly.TryParseExact(
                pricing.EffectiveFrom,
                "yyyy-MM-dd",
                CultureInfo.InvariantCulture,
                DateTimeStyles.None,
                out _
            ))
        {
            throw new InvalidDataException(
                $"模型“{modelId}”的 pricing.effective_from 必须是 yyyy-MM-dd。"
            );
        }
    }

    private static string DescribePricing(ModelPricing? pricing) => pricing is null
        ? string.Empty
        : $" · 参考价 输入¥{pricing.InputYuanPerMillion:0.##}/输出¥{pricing.OutputYuanPerMillion:0.##} 每百万 Token（{pricing.EffectiveFrom}）";

    private static ModelConfigurationDocument DeserializeAndValidate(string json)
    {
        try
        {
            RejectDuplicateProperties(Encoding.UTF8.GetBytes(json));
            var configuration = JsonSerializer.Deserialize<ModelConfigurationDocument>(
                json,
                ReadOptions
            ) ?? throw new InvalidDataException("模型配置不能为空。");
            Validate(configuration);
            return configuration;
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException(
                $"JSON 格式或字段无效（位置 {exception.Path ?? "未知"}）。",
                exception
            );
        }
    }

    private static void RejectDuplicateProperties(ReadOnlySpan<byte> json)
    {
        var reader = new Utf8JsonReader(json, new JsonReaderOptions
        {
            AllowTrailingCommas = false,
            CommentHandling = JsonCommentHandling.Disallow,
        });
        var objectProperties = new Stack<HashSet<string>>();
        while (reader.Read())
        {
            switch (reader.TokenType)
            {
                case JsonTokenType.StartObject:
                    objectProperties.Push(new HashSet<string>(StringComparer.Ordinal));
                    break;
                case JsonTokenType.EndObject:
                    objectProperties.Pop();
                    break;
                case JsonTokenType.PropertyName:
                    var propertyName = reader.GetString() ?? string.Empty;
                    if (objectProperties.Count == 0 ||
                        !objectProperties.Peek().Add(propertyName))
                    {
                        throw new InvalidDataException(
                            $"JSON 对象包含重复字段“{propertyName}”。"
                        );
                    }
                    break;
            }
        }
    }

    private static void RejectReparsePointIfPresent(string path)
    {
        try
        {
            var file = new FileInfo(path);
            file.Refresh();
            if (!file.Exists && file.LinkTarget is null)
            {
                return;
            }
            var attributes = File.GetAttributes(path);
            ValidateConfigurationFileAttributes(attributes);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or NotSupportedException)
        {
            throw new InvalidDataException("无法安全检查 models.json。", exception);
        }
    }

    internal static void ValidateConfigurationFileAttributes(FileAttributes attributes)
    {
        if ((attributes & FileAttributes.ReparsePoint) != 0 ||
            (attributes & FileAttributes.Directory) != 0)
        {
            throw new InvalidDataException(
                "models.json 不能是符号链接、重解析点或目录。"
            );
        }
    }

    [GeneratedRegex(@"^[a-z0-9][a-z0-9._:-]{0,127}$", RegexOptions.CultureInvariant)]
    private static partial Regex ModelIdPattern();
}
