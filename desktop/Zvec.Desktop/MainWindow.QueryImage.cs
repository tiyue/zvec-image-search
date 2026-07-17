using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Microsoft.Win32;
using Zvec.Desktop.Services;

namespace Zvec.Desktop;

public partial class MainWindow
{
    private readonly QueryImageInputService _queryImageInputService = new();
    private CancellationTokenSource? _queryImagePreviewCancellation;
    private string? _ownedQueryImagePath;
    private bool _queryImageInputBusy;

    private void InitializeQueryImageInput()
    {
        _ = DeleteStaleQueryImagesAsync();
        _ = RefreshQueryImagePreviewAsync();
    }

    private void DisposeQueryImageInput()
    {
        var cancellation = Interlocked.Exchange(
            ref _queryImagePreviewCancellation,
            null
        );
        cancellation?.Cancel();
        cancellation?.Dispose();
        if (_ownedQueryImagePath is not null)
        {
            _queryImageInputService.TryDeleteOwnedFile(_ownedQueryImagePath);
            _ownedQueryImagePath = null;
        }
    }

    private async Task DeleteStaleQueryImagesAsync()
    {
        try
        {
            await _queryImageInputService.DeleteStaleOwnedFilesAsync(
                TimeSpan.FromDays(2)
            );
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException
        )
        {
            AppendLog($"清理临时查询图片失败：{exception.Message}", isError: true);
        }
    }

    private void ChooseQueryImageFromFile(string? initialDirectory = null)
    {
        var dialog = new OpenFileDialog
        {
            Title = "选择查询图片",
            Filter = QueryImageInputService.FileDialogFilter,
            CheckFileExists = true,
            Multiselect = false,
        };
        if (Directory.Exists(initialDirectory))
        {
            dialog.InitialDirectory = initialDirectory;
        }
        else if (File.Exists(QueryImageTextBox.Text))
        {
            dialog.InitialDirectory = Path.GetDirectoryName(QueryImageTextBox.Text);
        }

        if (dialog.ShowDialog(this) == true)
        {
            ApplyQueryImagePath(dialog.FileName, ownsFile: false);
        }
    }

    private void BrowseQueryImageDirectory_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog
        {
            Title = "选择查询图片所在目录",
            Multiselect = false,
        };
        if (File.Exists(QueryImageTextBox.Text))
        {
            dialog.InitialDirectory = Path.GetDirectoryName(QueryImageTextBox.Text);
        }
        if (dialog.ShowDialog(this) == true)
        {
            ChooseQueryImageFromFile(dialog.FolderName);
        }
    }

    private async void PasteQueryImage_Click(object sender, RoutedEventArgs e) =>
        await PasteQueryImageAsync();

    private async void QueryImageDropZone_PreviewKeyDown(
        object sender,
        KeyEventArgs e)
    {
        if (e.Key == Key.V && Keyboard.Modifiers.HasFlag(ModifierKeys.Control))
        {
            e.Handled = true;
            await PasteQueryImageAsync();
        }
    }

    private async Task PasteQueryImageAsync()
    {
        if (_queryImageInputBusy)
        {
            return;
        }

        try
        {
            var dataObject = Clipboard.GetDataObject();
            if (dataObject is null || !await HandleQueryImageDataAsync(dataObject))
            {
                ShowValidation(
                    "剪贴板中没有可用图片、图片文件或目录。可以先复制图片，再点击“粘贴”。"
                );
            }
        }
        catch (ExternalException exception)
        {
            ShowValidation($"剪贴板暂时被其他程序占用，请稍后重试：{exception.Message}");
        }
    }

    private void QueryImageDropZone_DragOver(object sender, DragEventArgs e)
    {
        var canAccept = CanAcceptQueryImageData(e.Data);
        e.Effects = canAccept ? DragDropEffects.Copy : DragDropEffects.None;
        QueryImageDropOverlay.Visibility = canAccept
            ? Visibility.Visible
            : Visibility.Collapsed;
        e.Handled = true;
    }

    private void QueryImageDropZone_DragLeave(object sender, DragEventArgs e)
    {
        QueryImageDropOverlay.Visibility = Visibility.Collapsed;
        e.Handled = true;
    }

    private async void QueryImageDropZone_Drop(object sender, DragEventArgs e)
    {
        QueryImageDropOverlay.Visibility = Visibility.Collapsed;
        e.Handled = true;
        if (!await HandleQueryImageDataAsync(e.Data))
        {
            ShowValidation("拖放内容中没有可用图片。支持拖入图片文件或图片所在目录。");
        }
    }

    private static bool CanAcceptQueryImageData(IDataObject dataObject) =>
        dataObject.GetDataPresent(DataFormats.FileDrop) ||
        dataObject.GetDataPresent(DataFormats.Bitmap) ||
        dataObject.GetDataPresent(DataFormats.UnicodeText) ||
        dataObject.GetDataPresent(DataFormats.Text);

    private async Task<bool> HandleQueryImageDataAsync(IDataObject dataObject)
    {
        if (
            dataObject.GetDataPresent(DataFormats.FileDrop) &&
            dataObject.GetData(DataFormats.FileDrop) is string[] droppedPaths
        )
        {
            return HandleQueryImagePaths(droppedPaths);
        }

        if (
            dataObject.GetDataPresent(DataFormats.Bitmap) &&
            dataObject.GetData(DataFormats.Bitmap, autoConvert: true) is BitmapSource bitmap
        )
        {
            await SaveClipboardQueryImageAsync(bitmap);
            return true;
        }

        var text = dataObject.GetData(DataFormats.UnicodeText) as string ??
            dataObject.GetData(DataFormats.Text) as string;
        if (!string.IsNullOrWhiteSpace(text))
        {
            var paths = text.Split(
                ['\r', '\n'],
                StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries
            );
            return HandleQueryImagePaths(paths);
        }

        return false;
    }

    private bool HandleQueryImagePaths(IEnumerable<string> paths)
    {
        var resolution = _queryImageInputService.ResolveDrop(paths);
        if (resolution.HasImage)
        {
            ApplyQueryImagePath(resolution.ImagePath!, ownsFile: false);
            return true;
        }
        if (resolution.HasDirectory)
        {
            ChooseQueryImageFromFile(resolution.DirectoryPath);
            return true;
        }
        if (!string.IsNullOrWhiteSpace(resolution.ErrorMessage))
        {
            QueryImageInputStatusTextBlock.Text = resolution.ErrorMessage;
        }
        return false;
    }

    private async Task SaveClipboardQueryImageAsync(BitmapSource bitmap)
    {
        if (_queryImageInputBusy)
        {
            return;
        }

        _queryImageInputBusy = true;
        PasteQueryImageButton.IsEnabled = false;
        QueryImageInputStatusTextBlock.Text = "正在读取剪贴板图片…";
        try
        {
            var path = await _queryImageInputService.SaveClipboardImageAsync(bitmap);
            ApplyQueryImagePath(path, ownsFile: true);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or InvalidDataException or
                NotSupportedException or InvalidOperationException or ExternalException
        )
        {
            ShowValidation($"无法使用剪贴板图片：{exception.Message}");
        }
        finally
        {
            _queryImageInputBusy = false;
            PasteQueryImageButton.IsEnabled = true;
        }
    }

    private void ClearQueryImage_Click(object sender, RoutedEventArgs e)
    {
        ReleaseOwnedQueryImage();
        QueryImageTextBox.Clear();
        QueryImageDropZone.Focus();
    }

    private void QueryImageTextBox_TextChanged(
        object sender,
        System.Windows.Controls.TextChangedEventArgs e) =>
        _ = RefreshQueryImagePreviewAsync();

    private void ApplyQueryImagePath(string path, bool ownsFile)
    {
        var fullPath = Path.GetFullPath(path);
        if (
            _ownedQueryImagePath is not null &&
            !string.Equals(
                _ownedQueryImagePath,
                fullPath,
                StringComparison.OrdinalIgnoreCase
            )
        )
        {
            _queryImageInputService.TryDeleteOwnedFile(_ownedQueryImagePath);
        }
        _ownedQueryImagePath = ownsFile ? fullPath : null;
        if (string.Equals(
            QueryImageTextBox.Text,
            fullPath,
            StringComparison.OrdinalIgnoreCase
        ))
        {
            _ = RefreshQueryImagePreviewAsync();
        }
        else
        {
            QueryImageTextBox.Text = fullPath;
        }
        QueryImageDropZone.Focus();
    }

    private void ReleaseOwnedQueryImage()
    {
        if (_ownedQueryImagePath is null)
        {
            return;
        }
        _queryImageInputService.TryDeleteOwnedFile(_ownedQueryImagePath);
        _ownedQueryImagePath = null;
    }

    private async Task RefreshQueryImagePreviewAsync()
    {
        var previous = Interlocked.Exchange(
            ref _queryImagePreviewCancellation,
            new CancellationTokenSource()
        );
        previous?.Cancel();
        previous?.Dispose();
        var cancellation = _queryImagePreviewCancellation;
        if (cancellation is null)
        {
            return;
        }

        var path = QueryImageTextBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(path))
        {
            QueryImageInputPreviewImage.Source = null;
            QueryImageInputPreviewImage.Visibility = Visibility.Collapsed;
            QueryImageInputPlaceholder.Visibility = Visibility.Visible;
            QueryImageInputStatusTextBlock.Text =
                "拖入图片或文件夹，也可以选择图片、目录选图或粘贴。";
            return;
        }

        QueryImageInputStatusTextBlock.Text = "正在加载查询图片预览…";
        try
        {
            var source = await _queryImageInputService.LoadThumbnailAsync(
                path,
                cancellation.Token
            );
            if (cancellation.IsCancellationRequested)
            {
                return;
            }
            QueryImageInputPreviewImage.Source = source;
            QueryImageInputPreviewImage.Visibility = Visibility.Visible;
            QueryImageInputPlaceholder.Visibility = Visibility.Collapsed;
            QueryImageInputStatusTextBlock.Text =
                $"{Path.GetFileName(path)} · 可拖入或粘贴另一张图片替换";
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or
                InvalidDataException or ArgumentException or NotSupportedException or
                FormatException
        )
        {
            if (cancellation.IsCancellationRequested)
            {
                return;
            }
            QueryImageInputPreviewImage.Source = null;
            QueryImageInputPreviewImage.Visibility = Visibility.Collapsed;
            QueryImageInputPlaceholder.Visibility = Visibility.Visible;
            QueryImageInputStatusTextBlock.Text = exception.Message;
        }
    }
}
