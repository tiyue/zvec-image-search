using System.Text.Json;
using System.Windows.Media.Imaging;
using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

public sealed class SearchResultLoader
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    public async Task<SearchResultSession?> LoadFromCommandOutputAsync(
        LauncherConfig config,
        string standardOutput,
        CancellationToken cancellationToken = default)
    {
        if (
            TrailingJsonParser.TryDeserialize<SearchManifest>(standardOutput, out var report) &&
            report is not null &&
            !string.IsNullOrWhiteSpace(report.OutputDirectory)
        )
        {
            var directoryName = GetSafeLeafName(report.OutputDirectory);
            if (!string.IsNullOrWhiteSpace(directoryName))
            {
                var directory = GetDirectChild(config.ResultsDirectory, directoryName);
                var manifestPath = Path.Combine(directory, "results.json");
                for (var attempt = 0; attempt < 10 && !File.Exists(manifestPath); attempt++)
                {
                    await Task.Delay(100, cancellationToken);
                }
                if (File.Exists(manifestPath))
                {
                    return await LoadManifestAsync(
                        config,
                        manifestPath,
                        cancellationToken
                    ).ConfigureAwait(false);
                }
            }
        }

        return null;
    }

    public async Task<SearchResultSession?> LoadLatestAsync(
        LauncherConfig config,
        CancellationToken cancellationToken = default)
    {
        if (!Directory.Exists(config.ResultsDirectory))
        {
            return null;
        }

        var candidates = await Task.Run(
            () => Directory.EnumerateDirectories(config.ResultsDirectory)
                .Select(directory => Path.Combine(directory, "results.json"))
                .Where(File.Exists)
                .OrderByDescending(File.GetLastWriteTimeUtc)
                .Take(20)
                .ToList(),
            cancellationToken
        ).ConfigureAwait(false);
        foreach (var manifestPath in candidates)
        {
            try
            {
                return await LoadManifestAsync(
                    config,
                    manifestPath,
                    cancellationToken
                ).ConfigureAwait(false);
            }
            catch (JsonException)
            {
            }
            catch (IOException)
            {
            }
        }

        return null;
    }

    private static Task<SearchResultSession> LoadManifestAsync(
        LauncherConfig config,
        string manifestPath,
        CancellationToken cancellationToken) => Task.Run(async () =>
    {
        await using var stream = File.OpenRead(manifestPath);
        var manifest = await JsonSerializer.DeserializeAsync<SearchManifest>(
            stream,
            JsonOptions,
            cancellationToken
        ).ConfigureAwait(false) ?? throw new JsonException(
            $"无法解析搜索结果：{manifestPath}"
        );

        var directory = Path.GetDirectoryName(manifestPath)
            ?? throw new InvalidDataException("搜索结果目录无效。");
        var items = new List<SearchResultItem>(manifest.Results.Count);
        foreach (var hit in manifest.Results.OrderBy(hit => hit.Rank))
        {
            cancellationToken.ThrowIfCancellationRequested();
            items.Add(CreateItem(
                config,
                directory,
                hit,
                manifest.LowConfidenceOverride || string.Equals(
                    manifest.Status,
                    "low_confidence_override",
                    StringComparison.OrdinalIgnoreCase
                )
            ));
        }

        return new SearchResultSession
        {
            Manifest = manifest,
            Directory = directory,
            ManifestPath = manifestPath,
            Items = items,
        };
    }, cancellationToken);

    private static SearchResultItem CreateItem(
        LauncherConfig config,
        string resultDirectory,
        SearchManifestHit hit,
        bool isLowConfidenceOverride)
    {
        var copiedPath = GetDirectChild(resultDirectory, hit.CopiedFile);
        var libraryId = hit.LibraryId;
        if (string.IsNullOrWhiteSpace(libraryId) && config.Libraries.Count == 1)
        {
            libraryId = config.Libraries[0].Id;
        }
        var library = config.GetLibrary(libraryId);
        var libraryName = !string.IsNullOrWhiteSpace(hit.LibraryName)
            ? hit.LibraryName
            : library?.Name ?? libraryId;
        var originalPath = library is null
            ? null
            : TryResolveOriginalPath(library.ImageRoot, hit.RelativePath);
        return new SearchResultItem
        {
            Rank = hit.Rank,
            Name = Path.GetFileName(hit.RelativePath),
            RelativePath = hit.RelativePath,
            CopiedPath = copiedPath,
            OriginalPath = originalPath,
            DocumentId = hit.DocumentId,
            LibraryId = libraryId,
            LibraryName = libraryName,
            RootId = hit.RootId,
            Distance = hit.Distance,
            FusedScore = hit.FusedScore,
            RawScore = hit.RawScore,
            NormalizedScore = hit.NormalizedScore,
            Confidence = hit.Confidence,
            RankingConfidence = hit.RankingConfidence,
            MatchState = hit.MatchState,
            RankSource = hit.RankSource,
            ImageConfidence = hit.ImageConfidence,
            TextConfidence = hit.TextConfidence,
            MetadataConfidence = hit.MetadataConfidence,
            ImageRank = hit.ImageRank,
            TextRank = hit.TextRank,
            MetadataRank = hit.MetadataRank,
            RankAgreement = hit.RankAgreement,
            IsLowConfidenceOverride = isLowConfidenceOverride,
            Tags = hit.Tags,
            MatchedTags = hit.MatchedTags,
            Thumbnail = LoadThumbnail(copiedPath),
        };
    }

    private static BitmapSource? LoadThumbnail(string path)
    {
        try
        {
            using var stream = File.OpenRead(path);
            var image = new BitmapImage();
            image.BeginInit();
            image.CacheOption = BitmapCacheOption.OnLoad;
            image.CreateOptions = BitmapCreateOptions.IgnoreColorProfile;
            image.DecodePixelWidth = 360;
            image.StreamSource = stream;
            image.EndInit();
            image.Freeze();
            return image;
        }
        catch (Exception)
        {
            return null;
        }
    }

    private static string? TryResolveOriginalPath(string imageRoot, string relativePath)
    {
        try
        {
            var root = Path.GetFullPath(imageRoot).TrimEnd(Path.DirectorySeparatorChar);
            var relative = relativePath.Replace('/', Path.DirectorySeparatorChar);
            var candidate = Path.GetFullPath(Path.Combine(root, relative));
            var rootPrefix = root + Path.DirectorySeparatorChar;
            return candidate.StartsWith(rootPrefix, StringComparison.OrdinalIgnoreCase)
                ? candidate
                : null;
        }
        catch (Exception)
        {
            return null;
        }
    }

    private static string GetSafeLeafName(string path)
    {
        var normalized = path.Replace('\\', '/').TrimEnd('/');
        return normalized[(normalized.LastIndexOf('/') + 1)..];
    }

    private static string GetDirectChild(string root, string leaf)
    {
        if (string.IsNullOrWhiteSpace(leaf) || Path.IsPathRooted(leaf))
        {
            throw new InvalidDataException("搜索结果包含不安全的文件路径。");
        }

        var rootFull = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
        var candidate = Path.GetFullPath(Path.Combine(rootFull, leaf));
        var parent = Path.GetDirectoryName(candidate)?.TrimEnd(Path.DirectorySeparatorChar);
        if (!string.Equals(parent, rootFull, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("搜索结果文件路径越出了结果目录。");
        }

        return candidate;
    }
}
