using Zvec.Desktop.Collections;

internal static class SearchResultPagingContract
{
    public static void Run()
    {
        TestPagingAndBoundaries();
        TestShortAndEmptyResultSets();
        TestDirectPageNavigation();
        TestResultRefreshReturnsToFirstPage();
        TestInvalidArguments();
    }

    private static void TestPagingAndBoundaries()
    {
        var paging = new PagedCollectionState<int>(pageSize: 3);
        paging.ReplaceItems(Enumerable.Range(1, 8));

        Assert(paging.TotalItemCount == 8, "paging preserves the result count");
        Assert(paging.TotalPageCount == 3, "paging calculates the final partial page");
        Assert(paging.CurrentPageNumber == 1, "paging starts on the first page");
        Assert(
            paging.CurrentPageItems.SequenceEqual([1, 2, 3]),
            "first page contains the expected results"
        );
        Assert(!paging.CanMovePrevious, "first page disables previous navigation");
        Assert(paging.CanMoveNext, "first page enables next navigation");

        Assert(paging.MoveNext(), "next navigation changes the page");
        Assert(paging.CurrentPageNumber == 2, "next navigation updates the page number");
        Assert(
            paging.CurrentPageItems.SequenceEqual([4, 5, 6]),
            "second page contains the expected results"
        );

        Assert(paging.MoveNext(), "next navigation reaches the final page");
        Assert(paging.CurrentPageNumber == 3, "final page number is correct");
        Assert(
            paging.CurrentPageItems.SequenceEqual([7, 8]),
            "final page contains only the remaining results"
        );
        Assert(!paging.CanMoveNext, "final page disables next navigation");
        Assert(!paging.MoveNext(), "next navigation is a no-op at the final page");
        Assert(paging.CurrentPageNumber == 3, "failed next navigation preserves the page");

        Assert(paging.MovePrevious(), "previous navigation changes the page");
        Assert(paging.CurrentPageNumber == 2, "previous navigation updates the page number");
    }

    private static void TestShortAndEmptyResultSets()
    {
        var paging = new PagedCollectionState<string>(pageSize: 15);
        paging.ReplaceItems(["a.jpg", "b.jpg"]);

        Assert(paging.TotalPageCount == 1, "a short result set uses one page");
        Assert(paging.CurrentPageNumber == 1, "a short result set starts on page one");
        Assert(
            paging.CurrentPageItems.SequenceEqual(["a.jpg", "b.jpg"]),
            "a short result set remains intact"
        );
        Assert(
            !paging.CanMovePrevious && !paging.CanMoveNext,
            "a single page disables both navigation directions"
        );

        paging.ReplaceItems([]);
        Assert(paging.TotalItemCount == 0, "empty refresh clears the result count");
        Assert(paging.TotalPageCount == 0, "empty results have no pages");
        Assert(paging.CurrentPageNumber == 0, "empty results expose page number zero");
        Assert(paging.CurrentPageItems.Count == 0, "empty results expose no page items");
        Assert(
            !paging.CanMovePrevious && !paging.CanMoveNext,
            "empty results disable both navigation directions"
        );
    }

    private static void TestDirectPageNavigation()
    {
        var paging = new PagedCollectionState<int>(pageSize: 4);
        paging.ReplaceItems(Enumerable.Range(1, 10));

        Assert(paging.MoveToPage(3), "direct navigation reaches a requested page");
        Assert(paging.CurrentPageNumber == 3, "direct navigation updates the page number");
        Assert(
            paging.CurrentPageItems.SequenceEqual([9, 10]),
            "direct navigation exposes the requested page"
        );
        Assert(!paging.MoveToPage(3), "navigating to the current page is a no-op");
    }

    private static void TestResultRefreshReturnsToFirstPage()
    {
        var source = Enumerable.Range(1, 9).ToList();
        var paging = new PagedCollectionState<int>(pageSize: 3);
        paging.ReplaceItems(source);
        source.Add(10);
        Assert(
            paging.TotalItemCount == 9,
            "paging uses a stable snapshot instead of the caller's mutable collection"
        );
        paging.MoveToPage(3);

        paging.ReplaceItems([101, 102, 103, 104]);
        Assert(paging.CurrentPageNumber == 1, "a refreshed result set returns to page one");
        Assert(paging.TotalPageCount == 2, "refresh recalculates the page count");
        Assert(
            paging.CurrentPageItems.SequenceEqual([101, 102, 103]),
            "refresh exposes the new first page"
        );
    }

    private static void TestInvalidArguments()
    {
        AssertThrows<ArgumentOutOfRangeException>(
            () => _ = new PagedCollectionState<int>(pageSize: 0),
            "zero page size is rejected"
        );

        var paging = new PagedCollectionState<int>(pageSize: 3);
        AssertThrows<ArgumentNullException>(
            () => paging.ReplaceItems(null!),
            "a null result set is rejected"
        );
        paging.ReplaceItems([1, 2, 3, 4]);
        AssertThrows<ArgumentOutOfRangeException>(
            () => paging.MoveToPage(0),
            "page zero is rejected"
        );
        AssertThrows<ArgumentOutOfRangeException>(
            () => paging.MoveToPage(3),
            "a page after the final page is rejected"
        );
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException($"Contract failed: {message}");
        }
    }

    private static void AssertThrows<TException>(Action action, string message)
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

        throw new InvalidOperationException($"Contract failed: {message}");
    }
}
