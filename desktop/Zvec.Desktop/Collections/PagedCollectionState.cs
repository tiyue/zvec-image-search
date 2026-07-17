namespace Zvec.Desktop.Collections;

/// <summary>
/// Keeps a stable snapshot of a result set and exposes one page at a time.
/// The UI can copy <see cref="CurrentPageItems"/> into a range-aware observable
/// collection without duplicating page-boundary calculations in event handlers.
/// </summary>
internal sealed class PagedCollectionState<T>
{
    private IReadOnlyList<T> _items = [];
    private IReadOnlyList<T> _currentPageItems = [];
    private int _currentPageIndex;

    public PagedCollectionState(int pageSize)
    {
        if (pageSize <= 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(pageSize),
                pageSize,
                "Page size must be greater than zero."
            );
        }

        PageSize = pageSize;
    }

    public int PageSize { get; }

    public int TotalItemCount => _items.Count;

    public int TotalPageCount => TotalItemCount == 0
        ? 0
        : 1 + ((TotalItemCount - 1) / PageSize);

    /// <summary>
    /// Gets the one-based page number, or zero when the result set is empty.
    /// </summary>
    public int CurrentPageNumber => TotalPageCount == 0
        ? 0
        : _currentPageIndex + 1;

    public IReadOnlyList<T> CurrentPageItems => _currentPageItems;

    public bool CanMovePrevious => _currentPageIndex > 0;

    public bool CanMoveNext => _currentPageIndex + 1 < TotalPageCount;

    /// <summary>
    /// Replaces the complete result set and returns to the first page.
    /// A snapshot is taken so later changes to the caller's collection cannot
    /// invalidate page counts or page contents.
    /// </summary>
    public void ReplaceItems(IEnumerable<T> items)
    {
        ArgumentNullException.ThrowIfNull(items);
        _items = items.ToArray();
        _currentPageIndex = 0;
        RefreshCurrentPage();
    }

    public bool MovePrevious()
    {
        if (!CanMovePrevious)
        {
            return false;
        }

        _currentPageIndex--;
        RefreshCurrentPage();
        return true;
    }

    public bool MoveNext()
    {
        if (!CanMoveNext)
        {
            return false;
        }

        _currentPageIndex++;
        RefreshCurrentPage();
        return true;
    }

    public bool MoveToPage(int pageNumber)
    {
        if (pageNumber < 1 || pageNumber > TotalPageCount)
        {
            throw new ArgumentOutOfRangeException(
                nameof(pageNumber),
                pageNumber,
                $"Page number must be between 1 and {TotalPageCount}."
            );
        }

        var requestedPageIndex = pageNumber - 1;
        if (requestedPageIndex == _currentPageIndex)
        {
            return false;
        }

        _currentPageIndex = requestedPageIndex;
        RefreshCurrentPage();
        return true;
    }

    private void RefreshCurrentPage()
    {
        if (_items.Count == 0)
        {
            _currentPageItems = [];
            return;
        }

        var offset = _currentPageIndex * PageSize;
        var count = Math.Min(PageSize, _items.Count - offset);
        var page = new T[count];
        for (var index = 0; index < count; index++)
        {
            page[index] = _items[offset + index];
        }

        _currentPageItems = page;
    }
}
