using System.Windows.Media.Imaging;

namespace Zvec.Desktop.Services;

public sealed record QueryImageDropResolution(
    string? ImagePath,
    string? DirectoryPath,
    string? ErrorMessage
)
{
    public bool HasImage => !string.IsNullOrWhiteSpace(ImagePath);

    public bool HasDirectory => !string.IsNullOrWhiteSpace(DirectoryPath);
}

/// <summary>
/// Validates query-image input and owns clipboard images saved by the desktop UI.
/// User files are never copied or deleted; only files created below
/// <see cref="OwnedDirectory"/> can be removed by this service.
/// </summary>
public sealed class QueryImageInputService
{
    private const long MaximumClipboardPixels = 100_000_000;
    private const int ThumbnailDecodeWidth = 480;
    private static readonly HashSet<string> SupportedExtensions = new(
        [
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".bmp",
            ".tif",
            ".tiff",
            ".ico",
            ".dib",
            ".icns",
            ".sgi",
        ],
        StringComparer.OrdinalIgnoreCase
    );

    public const string FileDialogFilter =
        "图片文件|*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.tif;*.tiff;*.ico;*.dib;*.icns;*.sgi|" +
        "所有文件|*.*";

    public QueryImageInputService(string? ownedDirectory = null)
    {
        OwnedDirectory = Path.GetFullPath(
            string.IsNullOrWhiteSpace(ownedDirectory)
                ? Path.Combine(Path.GetTempPath(), "Zvec Desktop", "query-images")
                : ownedDirectory
        );
    }

    public string OwnedDirectory { get; }

    public bool IsSupportedImageFile(string? path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return false;
        }

        try
        {
            return SupportedExtensions.Contains(Path.GetExtension(path.Trim()));
        }
        catch (Exception exception) when (
            exception is ArgumentException or NotSupportedException or PathTooLongException
        )
        {
            return false;
        }
    }

    public QueryImageDropResolution ResolveDrop(IEnumerable<string> paths)
    {
        ArgumentNullException.ThrowIfNull(paths);
        string? firstDirectory = null;
        foreach (var rawPath in paths)
        {
            if (string.IsNullOrWhiteSpace(rawPath))
            {
                continue;
            }

            string fullPath;
            try
            {
                fullPath = Path.GetFullPath(rawPath.Trim().Trim('"'));
            }
            catch (Exception exception) when (
                exception is ArgumentException or NotSupportedException or PathTooLongException
            )
            {
                continue;
            }

            if (File.Exists(fullPath) && IsSupportedImageFile(fullPath))
            {
                return new QueryImageDropResolution(fullPath, null, null);
            }
            if (firstDirectory is null && Directory.Exists(fullPath))
            {
                firstDirectory = fullPath;
            }
        }

        return firstDirectory is not null
            ? new QueryImageDropResolution(null, firstDirectory, null)
            : new QueryImageDropResolution(
                null,
                null,
                "拖放内容中没有受支持的图片或可用目录。"
            );
    }

    public Task<BitmapSource> LoadThumbnailAsync(
        string path,
        CancellationToken cancellationToken = default)
    {
        var fullPath = ValidateExistingImage(path);
        return Task.Run(() =>
        {
            cancellationToken.ThrowIfCancellationRequested();
            using var stream = new FileStream(
                fullPath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete
            );
            var image = new BitmapImage();
            image.BeginInit();
            image.CacheOption = BitmapCacheOption.OnLoad;
            image.CreateOptions = BitmapCreateOptions.IgnoreColorProfile;
            image.DecodePixelWidth = ThumbnailDecodeWidth;
            image.StreamSource = stream;
            image.EndInit();
            cancellationToken.ThrowIfCancellationRequested();
            image.Freeze();
            return (BitmapSource)image;
        }, cancellationToken);
    }

    public async Task<string> SaveClipboardImageAsync(
        BitmapSource source,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(source);
        var pixels = (long)source.PixelWidth * source.PixelHeight;
        if (source.PixelWidth <= 0 || source.PixelHeight <= 0 || pixels > MaximumClipboardPixels)
        {
            throw new InvalidDataException(
                "剪贴板图片尺寸无效或过大，请先保存为图片文件后再选择。"
            );
        }

        // Clipboard objects are dispatcher-affine. Clone and freeze on the UI
        // thread before encoding in the background.
        var frozenSource = source.IsFrozen ? source : source.CloneCurrentValue();
        if (!frozenSource.IsFrozen)
        {
            frozenSource.Freeze();
        }
        var frame = BitmapFrame.Create(frozenSource);
        if (!frame.CanFreeze)
        {
            throw new InvalidDataException("剪贴板图片无法转换为可安全保存的位图。");
        }
        frame.Freeze();

        Directory.CreateDirectory(OwnedDirectory);
        var destination = Path.Combine(
            OwnedDirectory,
            $"query-{Guid.NewGuid():N}.png"
        );
        try
        {
            await Task.Run(() =>
            {
                cancellationToken.ThrowIfCancellationRequested();
                var encoder = new PngBitmapEncoder();
                encoder.Frames.Add(frame);
                using var stream = new FileStream(
                    destination,
                    FileMode.CreateNew,
                    FileAccess.Write,
                    FileShare.None
                );
                encoder.Save(stream);
                stream.Flush(flushToDisk: true);
            }, cancellationToken).ConfigureAwait(false);
            return destination;
        }
        catch
        {
            TryDeleteOwnedFile(destination);
            throw;
        }
    }

    public bool TryDeleteOwnedFile(string? path)
    {
        if (!TryResolveOwnedPath(path, out var fullPath))
        {
            return false;
        }

        try
        {
            File.Delete(fullPath);
            return !File.Exists(fullPath);
        }
        catch (IOException)
        {
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            return false;
        }
    }

    public Task<int> DeleteStaleOwnedFilesAsync(
        TimeSpan maximumAge,
        CancellationToken cancellationToken = default)
    {
        if (maximumAge < TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(
                nameof(maximumAge),
                maximumAge,
                "Maximum age cannot be negative."
            );
        }

        return Task.Run(() =>
        {
            if (!Directory.Exists(OwnedDirectory))
            {
                return 0;
            }

            var cutoff = DateTime.UtcNow - maximumAge;
            var deleted = 0;
            foreach (var path in Directory.EnumerateFiles(
                OwnedDirectory,
                "query-*.png",
                SearchOption.TopDirectoryOnly
            ))
            {
                cancellationToken.ThrowIfCancellationRequested();
                try
                {
                    if (File.GetLastWriteTimeUtc(path) < cutoff &&
                        TryDeleteOwnedFile(path))
                    {
                        deleted++;
                    }
                }
                catch (IOException)
                {
                }
                catch (UnauthorizedAccessException)
                {
                }
            }
            return deleted;
        }, cancellationToken);
    }

    private string ValidateExistingImage(string path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            throw new ArgumentException("查询图片路径不能为空。", nameof(path));
        }

        var fullPath = Path.GetFullPath(path.Trim().Trim('"'));
        if (!File.Exists(fullPath))
        {
            throw new FileNotFoundException("查询图片不存在。", fullPath);
        }
        if (!IsSupportedImageFile(fullPath))
        {
            throw new InvalidDataException(
                $"不支持该图片格式：{Path.GetExtension(fullPath)}"
            );
        }
        return fullPath;
    }

    private bool TryResolveOwnedPath(string? path, out string fullPath)
    {
        fullPath = string.Empty;
        if (string.IsNullOrWhiteSpace(path))
        {
            return false;
        }

        try
        {
            fullPath = Path.GetFullPath(path);
            var root = OwnedDirectory.TrimEnd(
                Path.DirectorySeparatorChar,
                Path.AltDirectorySeparatorChar
            ) + Path.DirectorySeparatorChar;
            return fullPath.StartsWith(root, StringComparison.OrdinalIgnoreCase) &&
                Path.GetFileName(fullPath).StartsWith(
                    "query-",
                    StringComparison.OrdinalIgnoreCase
                ) &&
                string.Equals(
                    Path.GetExtension(fullPath),
                    ".png",
                    StringComparison.OrdinalIgnoreCase
                );
        }
        catch (Exception exception) when (
            exception is ArgumentException or NotSupportedException or PathTooLongException
        )
        {
            fullPath = string.Empty;
            return false;
        }
    }
}
