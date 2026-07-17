using System.Windows.Media.Imaging;
using Zvec.Desktop.Models;
using Zvec.Desktop.Services;

namespace Zvec.Desktop;

public partial class MainWindow
{
    private const int SelectedPreviewDecodeWidth = 1_600;
    private readonly ImagePreviewLoader _selectedPreviewLoader = new();
    private CancellationTokenSource? _selectedPreviewCancellation;

    private async Task LoadSelectedResultPreviewAsync(SearchResultItem item)
    {
        var previous = Interlocked.Exchange(
            ref _selectedPreviewCancellation,
            new CancellationTokenSource()
        );
        previous?.Cancel();
        previous?.Dispose();
        var cancellation = _selectedPreviewCancellation;
        if (cancellation is null)
        {
            return;
        }

        var path = File.Exists(item.OriginalPath)
            ? item.OriginalPath
            : item.CopiedPath;
        try
        {
            var preview = await _selectedPreviewLoader.LoadAsync(
                path,
                SelectedPreviewDecodeWidth,
                cancellation.Token
            );
            if (
                cancellation.IsCancellationRequested ||
                !ReferenceEquals(ResultsList.SelectedItem, item) ||
                preview is null
            )
            {
                return;
            }
            SelectedPreviewImage.Source = preview;
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or
                NotSupportedException or FormatException
        )
        {
            // The result card thumbnail remains visible if the full preview cannot
            // be read, so one corrupt or locked file never interrupts browsing.
            AppendLog(
                $"无法加载大图预览 {Path.GetFileName(path)}：{exception.Message}",
                isError: true
            );
        }
    }

    private void CancelSelectedResultPreview()
    {
        var cancellation = Interlocked.Exchange(
            ref _selectedPreviewCancellation,
            null
        );
        cancellation?.Cancel();
        cancellation?.Dispose();
    }
}
