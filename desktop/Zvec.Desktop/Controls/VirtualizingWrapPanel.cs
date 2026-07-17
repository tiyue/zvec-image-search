using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;

namespace Zvec.Desktop.Controls;

/// <summary>
/// A wrapping panel that realizes only the rows visible in its ScrollViewer.
/// It uses fixed minimum cells for large result sets and can stretch one paged result
/// set to consume the viewport without sacrificing the small-window scroll fallback.
/// </summary>
public sealed class VirtualizingWrapPanel : VirtualizingPanel, IScrollInfo
{
    public static readonly DependencyProperty ItemWidthProperty = DependencyProperty.Register(
        nameof(ItemWidth),
        typeof(double),
        typeof(VirtualizingWrapPanel),
        new FrameworkPropertyMetadata(
            160d,
            FrameworkPropertyMetadataOptions.AffectsMeasure
        ),
        IsValidItemDimension
    );

    public static readonly DependencyProperty ItemHeightProperty = DependencyProperty.Register(
        nameof(ItemHeight),
        typeof(double),
        typeof(VirtualizingWrapPanel),
        new FrameworkPropertyMetadata(
            236d,
            FrameworkPropertyMetadataOptions.AffectsMeasure
        ),
        IsValidItemDimension
    );

    public static readonly DependencyProperty StretchItemsToViewportProperty =
        DependencyProperty.Register(
            nameof(StretchItemsToViewport),
            typeof(bool),
            typeof(VirtualizingWrapPanel),
            new FrameworkPropertyMetadata(
                false,
                FrameworkPropertyMetadataOptions.AffectsMeasure
            )
        );

    public static readonly DependencyProperty ExactFillItemCountProperty =
        DependencyProperty.Register(
            nameof(ExactFillItemCount),
            typeof(int),
            typeof(VirtualizingWrapPanel),
            new FrameworkPropertyMetadata(
                0,
                FrameworkPropertyMetadataOptions.AffectsMeasure
            ),
            IsValidExactFillItemCount
        );

    private Size _extent;
    private Size _viewport;
    private Point _offset;
    private int _itemsPerRow = 1;
    private double _layoutItemWidth = 160d;
    private double _layoutItemHeight = 236d;

    public double ItemWidth
    {
        get => (double)GetValue(ItemWidthProperty);
        set => SetValue(ItemWidthProperty, value);
    }

    public double ItemHeight
    {
        get => (double)GetValue(ItemHeightProperty);
        set => SetValue(ItemHeightProperty, value);
    }

    public bool StretchItemsToViewport
    {
        get => (bool)GetValue(StretchItemsToViewportProperty);
        set => SetValue(StretchItemsToViewportProperty, value);
    }

    /// <summary>
    /// When the current page contains this many items, only a gap-free grid is
    /// stretched to the viewport. If the minimum card size cannot be preserved,
    /// the panel keeps fixed-size cards and falls back to vertical scrolling.
    /// </summary>
    public int ExactFillItemCount
    {
        get => (int)GetValue(ExactFillItemCountProperty);
        set => SetValue(ExactFillItemCountProperty, value);
    }

    public bool CanHorizontallyScroll { get; set; }

    public bool CanVerticallyScroll { get; set; } = true;

    public double ExtentWidth => _extent.Width;

    public double ExtentHeight => _extent.Height;

    public double ViewportWidth => _viewport.Width;

    public double ViewportHeight => _viewport.Height;

    public double HorizontalOffset => _offset.X;

    public double VerticalOffset => _offset.Y;

    public ScrollViewer? ScrollOwner { get; set; }

    protected override Size MeasureOverride(Size availableSize)
    {
        var owner = ItemsControl.GetItemsOwner(this);
        var itemCount = owner?.Items.Count ?? 0;
        var viewportWidth = double.IsInfinity(availableSize.Width)
            ? Math.Max(ItemWidth, _viewport.Width)
            : Math.Max(0, availableSize.Width);
        var viewportHeight = double.IsInfinity(availableSize.Height)
            ? Math.Max(ItemHeight, _viewport.Height)
            : Math.Max(0, availableSize.Height);
        _layoutItemWidth = ItemWidth;
        _layoutItemHeight = ItemHeight;
        _itemsPerRow = Math.Max(1, (int)Math.Floor(viewportWidth / ItemWidth));
        var rowCount = itemCount == 0
            ? 0
            : (itemCount + _itemsPerRow - 1) / _itemsPerRow;
        if (
            TryConfigureStretchedLayout(
                itemCount,
                viewportWidth,
                viewportHeight,
                out var stretchedRows
            )
        )
        {
            rowCount = stretchedRows;
        }
        var extentHeight = rowCount * _layoutItemHeight;
        if (double.IsInfinity(availableSize.Height))
        {
            viewportHeight = extentHeight;
        }

        UpdateScrollInfo(
            new Size(viewportWidth, extentHeight),
            new Size(viewportWidth, viewportHeight)
        );

        if (itemCount == 0)
        {
            RecycleOutsideRange(0, -1);
            return new Size(viewportWidth, viewportHeight);
        }

        var firstVisibleRow = Math.Max(
            0,
            (int)Math.Floor(VerticalOffset / _layoutItemHeight)
        );
        var visibleRows = double.IsInfinity(viewportHeight) || viewportHeight <= 0
            ? rowCount
            : Math.Max(1, (int)Math.Ceiling(viewportHeight / _layoutItemHeight));
        // Retain one row on either side so a mouse-wheel step does not flash empty cards.
        var firstRealizedRow = Math.Max(0, firstVisibleRow - 1);
        var lastRealizedRow = Math.Min(rowCount - 1, firstVisibleRow + visibleRows);
        var firstIndex = firstRealizedRow * _itemsPerRow;
        var lastIndex = Math.Min(itemCount - 1, ((lastRealizedRow + 1) * _itemsPerRow) - 1);

        RealizeRange(firstIndex, lastIndex);
        RecycleOutsideRange(firstIndex, lastIndex);
        return new Size(viewportWidth, viewportHeight);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        var generator = GetGenerator();
        if (generator is null)
        {
            return finalSize;
        }
        for (var childIndex = 0; childIndex < InternalChildren.Count; childIndex++)
        {
            var itemIndex = generator.IndexFromGeneratorPosition(
                new GeneratorPosition(childIndex, 0)
            );
            if (itemIndex < 0)
            {
                continue;
            }

            var row = itemIndex / _itemsPerRow;
            var column = itemIndex % _itemsPerRow;
            InternalChildren[childIndex].Arrange(new Rect(
                column * _layoutItemWidth,
                (row * _layoutItemHeight) - VerticalOffset,
                _layoutItemWidth,
                _layoutItemHeight
            ));
        }
        return finalSize;
    }

    protected override void OnItemsChanged(object sender, ItemsChangedEventArgs args)
    {
        base.OnItemsChanged(sender, args);
        if (args.Action == System.Collections.Specialized.NotifyCollectionChangedAction.Reset &&
            InternalChildren.Count > 0)
        {
            RemoveInternalChildRange(0, InternalChildren.Count);
        }
        InvalidateMeasure();
    }

    public void LineUp() => SetVerticalOffset(VerticalOffset - 24);

    public void LineDown() => SetVerticalOffset(VerticalOffset + 24);

    public void LineLeft()
    {
    }

    public void LineRight()
    {
    }

    public void MouseWheelUp() =>
        SetVerticalOffset(VerticalOffset - (_layoutItemHeight / 2));

    public void MouseWheelDown() =>
        SetVerticalOffset(VerticalOffset + (_layoutItemHeight / 2));

    public void MouseWheelLeft()
    {
    }

    public void MouseWheelRight()
    {
    }

    public void PageUp() => SetVerticalOffset(VerticalOffset - ViewportHeight);

    public void PageDown() => SetVerticalOffset(VerticalOffset + ViewportHeight);

    public void PageLeft()
    {
    }

    public void PageRight()
    {
    }

    public void SetHorizontalOffset(double offset)
    {
        if (offset != 0)
        {
            _offset.X = 0;
            ScrollOwner?.InvalidateScrollInfo();
        }
    }

    public void SetVerticalOffset(double offset)
    {
        var coerced = CoerceOffset(offset, ExtentHeight, ViewportHeight);
        if (AreClose(coerced, _offset.Y))
        {
            return;
        }
        _offset.Y = coerced;
        ScrollOwner?.InvalidateScrollInfo();
        InvalidateMeasure();
    }

    public Rect MakeVisible(Visual visual, Rect rectangle)
    {
        var container = visual as DependencyObject;
        while (container is not null &&
               !ReferenceEquals(VisualTreeHelper.GetParent(container), this))
        {
            container = VisualTreeHelper.GetParent(container);
        }
        if (container is not UIElement element)
        {
            return Rect.Empty;
        }

        var childIndex = InternalChildren.IndexOf(element);
        var index = childIndex < 0
            ? -1
            : GetGenerator()?.IndexFromGeneratorPosition(
                new GeneratorPosition(childIndex, 0)
            ) ?? -1;
        if (index < 0)
        {
            return Rect.Empty;
        }
        var top = (index / _itemsPerRow) * _layoutItemHeight;
        var bottom = top + _layoutItemHeight;
        if (top < VerticalOffset)
        {
            SetVerticalOffset(top);
        }
        else if (bottom > VerticalOffset + ViewportHeight)
        {
            SetVerticalOffset(bottom - ViewportHeight);
        }
        return new Rect(
            0,
            top - VerticalOffset,
            _layoutItemWidth,
            _layoutItemHeight
        );
    }

    private void RealizeRange(int firstIndex, int lastIndex)
    {
        if (firstIndex > lastIndex)
        {
            return;
        }
        var generator = GetGenerator();
        if (generator is null)
        {
            return;
        }
        var startPosition = generator.GeneratorPositionFromIndex(firstIndex);
        var childIndex = startPosition.Offset == 0
            ? startPosition.Index
            : startPosition.Index + 1;
        using (generator.StartAt(
            startPosition,
            GeneratorDirection.Forward,
            allowStartAtRealizedItem: true
        ))
        {
            for (var itemIndex = firstIndex; itemIndex <= lastIndex; itemIndex++, childIndex++)
            {
                var child = (UIElement)generator.GenerateNext(out var isNewlyRealized);
                if (isNewlyRealized)
                {
                    if (childIndex >= InternalChildren.Count)
                    {
                        AddInternalChild(child);
                    }
                    else
                    {
                        InsertInternalChild(childIndex, child);
                    }
                    generator.PrepareItemContainer(child);
                }
                child.Measure(new Size(_layoutItemWidth, _layoutItemHeight));
            }
        }
    }

    private bool TryConfigureStretchedLayout(
        int itemCount,
        double viewportWidth,
        double viewportHeight,
        out int rowCount)
    {
        rowCount = 0;
        if (
            !StretchItemsToViewport ||
            itemCount <= 0 ||
            viewportWidth <= 0 ||
            viewportHeight <= 0 ||
            double.IsInfinity(viewportWidth) ||
            double.IsInfinity(viewportHeight)
        )
        {
            return false;
        }

        var requireExactFill =
            ExactFillItemCount > 0 &&
            itemCount == ExactFillItemCount;
        var layout = CalculateStretchedLayout(
            itemCount,
            viewportWidth,
            viewportHeight,
            ItemWidth,
            ItemHeight,
            requireExactFill
        );
        if (layout is null)
        {
            return false;
        }

        _itemsPerRow = layout.Value.Columns;
        _layoutItemWidth = layout.Value.ItemWidth;
        _layoutItemHeight = layout.Value.ItemHeight;
        rowCount = layout.Value.Rows;
        return true;
    }

    internal static StretchedLayout? CalculateStretchedLayout(
        int itemCount,
        double viewportWidth,
        double viewportHeight,
        double minimumItemWidth,
        double minimumItemHeight,
        bool requireExactFill)
    {
        if (
            itemCount <= 0 ||
            viewportWidth <= 0 ||
            viewportHeight <= 0 ||
            minimumItemWidth <= 0 ||
            minimumItemHeight <= 0 ||
            double.IsInfinity(viewportWidth) ||
            double.IsInfinity(viewportHeight) ||
            double.IsNaN(viewportWidth) ||
            double.IsNaN(viewportHeight)
        )
        {
            return null;
        }

        var maximumColumns = Math.Min(
            itemCount,
            Math.Max(1, (int)Math.Floor(viewportWidth / minimumItemWidth))
        );
        var targetAspect = minimumItemWidth / minimumItemHeight;
        var bestScore = double.PositiveInfinity;
        var bestColumns = 0;
        var bestRows = 0;
        var bestWidth = 0d;
        var bestHeight = 0d;
        for (var columns = 1; columns <= maximumColumns; columns++)
        {
            var rows = (itemCount + columns - 1) / columns;
            var emptyCells = (columns * rows) - itemCount;
            if (requireExactFill && emptyCells != 0)
            {
                continue;
            }

            var candidateWidth = viewportWidth / columns;
            var candidateHeight = viewportHeight / rows;
            if (
                candidateWidth + 0.1 < minimumItemWidth ||
                candidateHeight + 0.1 < minimumItemHeight
            )
            {
                continue;
            }

            var actualAspect = candidateWidth / candidateHeight;
            var aspectPenalty = Math.Abs(Math.Log(actualAspect / targetAspect));
            var emptyPenalty = emptyCells / (double)itemCount * 0.75;
            var score = aspectPenalty + emptyPenalty;
            if (score >= bestScore)
            {
                continue;
            }

            bestScore = score;
            bestColumns = columns;
            bestRows = rows;
            bestWidth = candidateWidth;
            bestHeight = candidateHeight;
        }

        if (bestColumns == 0)
        {
            return null;
        }

        return new StretchedLayout(
            bestColumns,
            bestRows,
            bestWidth,
            bestHeight
        );
    }

    private void RecycleOutsideRange(int firstIndex, int lastIndex)
    {
        var generator = GetGenerator();
        if (generator is null)
        {
            return;
        }
        for (var childIndex = InternalChildren.Count - 1; childIndex >= 0; childIndex--)
        {
            var itemIndex = generator.IndexFromGeneratorPosition(
                new GeneratorPosition(childIndex, 0)
            );
            if (itemIndex < 0)
            {
                continue;
            }
            if (itemIndex >= firstIndex && itemIndex <= lastIndex)
            {
                continue;
            }

            // ItemContainerGenerator's recycling path assumes framework-owned
            // virtualization bookkeeping that a custom panel does not receive.
            // Removing here is still bounded to off-screen rows and is reliable for
            // arbitrary ListBox templates; visible containers remain untouched.
            generator.Remove(generator.GeneratorPositionFromIndex(itemIndex), 1);
            RemoveInternalChildRange(childIndex, 1);
        }
    }

    private void UpdateScrollInfo(Size extent, Size viewport)
    {
        var changed = !AreClose(extent.Width, _extent.Width) ||
            !AreClose(extent.Height, _extent.Height) ||
            !AreClose(viewport.Width, _viewport.Width) ||
            !AreClose(viewport.Height, _viewport.Height);
        _extent = extent;
        _viewport = viewport;
        _offset.X = 0;
        _offset.Y = CoerceOffset(_offset.Y, extent.Height, viewport.Height);
        if (changed)
        {
            ScrollOwner?.InvalidateScrollInfo();
        }
    }

    private static double CoerceOffset(double offset, double extent, double viewport)
    {
        if (double.IsNaN(offset) || offset < 0)
        {
            return 0;
        }
        return Math.Min(offset, Math.Max(0, extent - viewport));
    }

    private static bool IsValidItemDimension(object value) =>
        value is double dimension &&
        dimension > 0 &&
        !double.IsInfinity(dimension) &&
        !double.IsNaN(dimension);

    private static bool IsValidExactFillItemCount(object value) =>
        value is int count && count >= 0;

    private static bool AreClose(double left, double right) =>
        Math.Abs(left - right) < 0.1;

    private IItemContainerGenerator? GetGenerator()
    {
        var ownerGenerator = ItemsControl.GetItemsOwner(this)?.ItemContainerGenerator;
        return ((IItemContainerGenerator?)ownerGenerator)?
            .GetItemContainerGeneratorForPanel(this);
    }

    internal readonly record struct StretchedLayout(
        int Columns,
        int Rows,
        double ItemWidth,
        double ItemHeight
    );
}
