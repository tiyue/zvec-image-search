using System.Windows.Media.Imaging;

namespace Zvec.Desktop.Services;

/// <summary>
/// Loads one bounded, file-detached preview away from the UI thread.
/// </summary>
public sealed class ImagePreviewLoader
{
    public Task<BitmapSource?> LoadAsync(
        string? path,
        int decodePixelWidth,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            return Task.FromResult<BitmapSource?>(null);
        }
        if (decodePixelWidth <= 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(decodePixelWidth),
                decodePixelWidth,
                "Preview width must be greater than zero."
            );
        }

        var fullPath = Path.GetFullPath(path);
        return Task.Run<BitmapSource?>(() =>
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
            image.DecodePixelWidth = decodePixelWidth;
            image.StreamSource = stream;
            image.EndInit();
            cancellationToken.ThrowIfCancellationRequested();
            image.Freeze();
            return image;
        }, cancellationToken);
    }
}
