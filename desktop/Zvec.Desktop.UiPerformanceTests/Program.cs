using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Zvec.Desktop.Controls;
using Zvec.Desktop.Models;
using Zvec.Desktop.Services;

namespace Zvec.Desktop.UiPerformanceTests;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        TestTaskItemSuppressesEquivalentSnapshots();
        TestCustomModelPolicyUsesNeutralLabels();
        TestResultPanelVirtualizesLargeLists();
        TestPagedResultPanelFillsViewport();
        TestPagedResultPanelFallsBackToScrolling();
        TestClipboardImageLifecycle();
        Console.WriteLine("Desktop UI performance tests passed.");
    }

    private static void TestCustomModelPolicyUsesNeutralLabels()
    {
        var proposal = new AutoTagProposal
        {
            ModelTrace = ["future:vl-fast"],
            ResolvedModel = "future:vl-fast",
        };
        Assert(proposal.ModelPolicySummary.Contains("future:vl-fast", StringComparison.Ordinal),
            "custom model ID remains visible");
        Assert(proposal.ModelPolicySummary.Contains("主模型完成", StringComparison.Ordinal),
            "custom model completion uses neutral wording");
        Assert(!proposal.ModelPolicySummary.Contains("Flash 完成", StringComparison.Ordinal),
            "custom model is not mislabeled as Flash");
    }

    private static void TestTaskItemSuppressesEquivalentSnapshots()
    {
        var item = new BackendTaskItem();
        var notifications = 0;
        item.PropertyChanged += (_, _) => notifications++;
        Assert(item.Update(CreateJob(current: 10)), "first snapshot changes the row");
        Assert(!item.Update(CreateJob(current: 10)), "equivalent snapshot is suppressed");
        Assert(item.Update(CreateJob(current: 11)), "new progress changes the row");
        Assert(notifications == 2, "only changed snapshots notify WPF bindings");
        Console.WriteLine("Task-row notifications: 2 for 3 snapshots (1 equivalent suppressed).");
    }

    private static void TestResultPanelVirtualizesLargeLists()
    {
        var panelFactory = new FrameworkElementFactory(typeof(VirtualizingWrapPanel));
        panelFactory.SetValue(VirtualizingWrapPanel.ItemWidthProperty, 160d);
        panelFactory.SetValue(VirtualizingWrapPanel.ItemHeightProperty, 236d);
        var list = new ListBox
        {
            Width = 660,
            Height = 500,
            ItemsPanel = new ItemsPanelTemplate(panelFactory),
            ItemsSource = Enumerable.Range(0, 1_000).ToArray(),
        };
        ScrollViewer.SetCanContentScroll(list, true);
        ScrollViewer.SetVerticalScrollBarVisibility(list, ScrollBarVisibility.Auto);
        VirtualizingPanel.SetIsVirtualizing(list, true);
        VirtualizingPanel.SetVirtualizationMode(list, VirtualizationMode.Recycling);

        var window = new Window
        {
            Width = 680,
            Height = 540,
            Content = list,
            Opacity = 0,
            ShowInTaskbar = false,
            WindowStyle = WindowStyle.None,
        };
        try
        {
            window.Show();
            window.UpdateLayout();
            var panel = FindDescendant<VirtualizingWrapPanel>(list)
                ?? throw new InvalidOperationException("VirtualizingWrapPanel was not created.");
            var firstRealized = VisualTreeHelper.GetChildrenCount(panel);
            Assert(firstRealized is > 0 and < 40,
                $"initial viewport realizes a bounded card set (actual {firstRealized})");

            var viewer = FindDescendant<ScrollViewer>(list)
                ?? throw new InvalidOperationException("ListBox ScrollViewer was not created.");
            viewer.ScrollToEnd();
            window.UpdateLayout();
            var lastRealized = VisualTreeHelper.GetChildrenCount(panel);
            Assert(lastRealized is > 0 and < 40,
                $"last viewport remains virtualized (actual {lastRealized})");
            Console.WriteLine(
                $"Virtualized result cards: initial={firstRealized}, end={lastRealized}, total=1000."
            );
        }
        finally
        {
            window.Close();
        }
    }

    private static void TestPagedResultPanelFillsViewport()
    {
        var panelFactory = new FrameworkElementFactory(typeof(VirtualizingWrapPanel));
        panelFactory.SetValue(VirtualizingWrapPanel.ItemWidthProperty, 160d);
        panelFactory.SetValue(VirtualizingWrapPanel.ItemHeightProperty, 236d);
        panelFactory.SetValue(
            VirtualizingWrapPanel.StretchItemsToViewportProperty,
            true
        );
        var list = new ListBox
        {
            Width = 1_100,
            Height = 800,
            ItemsPanel = new ItemsPanelTemplate(panelFactory),
            ItemsSource = Enumerable.Range(0, 15).ToArray(),
        };
        ScrollViewer.SetCanContentScroll(list, true);
        ScrollViewer.SetVerticalScrollBarVisibility(list, ScrollBarVisibility.Auto);
        VirtualizingPanel.SetIsVirtualizing(list, true);

        var window = new Window
        {
            Width = 1_120,
            Height = 840,
            Content = list,
            Opacity = 0,
            ShowInTaskbar = false,
            WindowStyle = WindowStyle.None,
        };
        try
        {
            window.Show();
            window.UpdateLayout();
            var panel = FindDescendant<VirtualizingWrapPanel>(list)
                ?? throw new InvalidOperationException("Stretch result panel was not created.");
            // The first pass realizes containers; the second pass arranges those
            // newly generated children into their stretched cells.
            window.UpdateLayout();
            Assert(
                Math.Abs(panel.ExtentHeight - panel.ViewportHeight) < 1,
                "a 15-item page consumes the available viewport height"
            );
            Assert(
                VisualTreeHelper.GetChildrenCount(panel) == 15,
                "all items on the current page are realized"
            );
            var first = (FrameworkElement)VisualTreeHelper.GetChild(panel, 0);
            Console.WriteLine(
                $"Stretch diagnostics: type={first.GetType().Name}, " +
                $"card={first.ActualWidth:F0}x{first.ActualHeight:F0}, " +
                $"desired={first.DesiredSize.Width:F0}x{first.DesiredSize.Height:F0}, " +
                $"render={first.RenderSize.Width:F0}x{first.RenderSize.Height:F0}, " +
                $"minimum={panel.ItemWidth:F0}x{panel.ItemHeight:F0}, " +
                $"extent={panel.ExtentWidth:F0}x{panel.ExtentHeight:F0}, " +
                $"viewport={panel.ViewportWidth:F0}x{panel.ViewportHeight:F0}."
            );
            Assert(
                first.ActualWidth > panel.ItemWidth &&
                    first.ActualHeight > panel.ItemHeight,
                $"page cards grow beyond their minimum size to fill the gallery " +
                    $"(actual {first.ActualWidth:F0}x{first.ActualHeight:F0})"
            );
            Console.WriteLine(
                $"Filled result page: card={first.ActualWidth:F0}x{first.ActualHeight:F0}, " +
                $"viewport={panel.ViewportWidth:F0}x{panel.ViewportHeight:F0}."
            );
        }
        finally
        {
            window.Close();
        }
    }

    private static void TestPagedResultPanelFallsBackToScrolling()
    {
        var panelFactory = new FrameworkElementFactory(typeof(VirtualizingWrapPanel));
        panelFactory.SetValue(VirtualizingWrapPanel.ItemWidthProperty, 160d);
        panelFactory.SetValue(VirtualizingWrapPanel.ItemHeightProperty, 236d);
        panelFactory.SetValue(
            VirtualizingWrapPanel.StretchItemsToViewportProperty,
            true
        );
        var list = new ListBox
        {
            Width = 660,
            Height = 500,
            ItemsPanel = new ItemsPanelTemplate(panelFactory),
            ItemsSource = Enumerable.Range(0, 15).ToArray(),
        };
        ScrollViewer.SetCanContentScroll(list, true);
        ScrollViewer.SetVerticalScrollBarVisibility(list, ScrollBarVisibility.Auto);
        VirtualizingPanel.SetIsVirtualizing(list, true);

        var window = new Window
        {
            Width = 680,
            Height = 540,
            Content = list,
            Opacity = 0,
            ShowInTaskbar = false,
            WindowStyle = WindowStyle.None,
        };
        try
        {
            window.Show();
            window.UpdateLayout();
            var panel = FindDescendant<VirtualizingWrapPanel>(list)
                ?? throw new InvalidOperationException("Small result panel was not created.");
            var viewer = FindDescendant<ScrollViewer>(list)
                ?? throw new InvalidOperationException("Small result ScrollViewer was not created.");
            window.UpdateLayout();

            Assert(
                panel.ExtentHeight > panel.ViewportHeight,
                "a small gallery keeps minimum card height and exposes vertical scrolling"
            );
            Assert(
                viewer.ScrollableHeight > 0,
                "the small-window result gallery has a usable scroll range"
            );
            var first = (FrameworkElement)VisualTreeHelper.GetChild(panel, 0);
            Assert(
                Math.Abs(first.ActualWidth - panel.ItemWidth) < 1 &&
                    Math.Abs(first.ActualHeight - panel.ItemHeight) < 1,
                "small-window cards retain their readable minimum size"
            );
            Console.WriteLine(
                $"Scrollable result page: card={first.ActualWidth:F0}x{first.ActualHeight:F0}, " +
                $"extent={panel.ExtentHeight:F0}, viewport={panel.ViewportHeight:F0}."
            );
        }
        finally
        {
            window.Close();
        }
    }

    private static void TestClipboardImageLifecycle()
    {
        var root = Path.Combine(
            Path.GetTempPath(),
            $"zvec-query-bitmap-test-{Guid.NewGuid():N}"
        );
        var service = new QueryImageInputService(root);
        try
        {
            var pixels = new byte[]
            {
                0x20, 0x40, 0x80, 0xFF,
                0x30, 0x50, 0x90, 0xFF,
                0x40, 0x60, 0xA0, 0xFF,
                0x50, 0x70, 0xB0, 0xFF,
            };
            var bitmap = BitmapSource.Create(
                2,
                2,
                96,
                96,
                PixelFormats.Bgra32,
                null,
                pixels,
                stride: 8
            );
            bitmap.Freeze();
            var path = service.SaveClipboardImageAsync(bitmap)
                .GetAwaiter()
                .GetResult();
            Assert(File.Exists(path), "clipboard bitmap is persisted as an owned PNG");
            var thumbnail = service.LoadThumbnailAsync(path)
                .GetAwaiter()
                .GetResult();
            Assert(
                thumbnail.PixelWidth > 0 && thumbnail.PixelHeight > 0,
                "persisted clipboard bitmap can be decoded for preview"
            );
            var fullPreview = new ImagePreviewLoader().LoadAsync(path, 1_600)
                .GetAwaiter()
                .GetResult();
            Assert(
                fullPreview is { PixelWidth: > 0, PixelHeight: > 0 },
                "selected-result preview loader decodes a detached larger image"
            );
            Assert(service.TryDeleteOwnedFile(path), "owned clipboard bitmap is removable");
        }
        finally
        {
            if (Directory.Exists(root))
            {
                Directory.Delete(root, recursive: true);
            }
        }
    }

    private static BackendJob CreateJob(int current) => new()
    {
        Id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        Command = "index",
        Status = "running",
        Progress = new BackendJobProgress
        {
            Message = $"正在处理 {current}/100",
            Current = current,
            Total = 100,
        },
    };

    private static T? FindDescendant<T>(DependencyObject root)
        where T : DependencyObject
    {
        for (var index = 0; index < VisualTreeHelper.GetChildrenCount(root); index++)
        {
            var child = VisualTreeHelper.GetChild(root, index);
            if (child is T match)
            {
                return match;
            }
            var nested = FindDescendant<T>(child);
            if (nested is not null)
            {
                return nested;
            }
        }
        return null;
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException($"Assertion failed: {message}");
        }
    }
}
