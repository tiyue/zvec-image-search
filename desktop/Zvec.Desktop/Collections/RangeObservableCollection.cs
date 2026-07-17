using System.Collections.ObjectModel;
using System.Collections.Specialized;
using System.ComponentModel;

namespace Zvec.Desktop.Collections;

/// <summary>
/// Replaces a collection with a single reset notification. This keeps WPF from
/// measuring and arranging the list once for every item in a freshly loaded page.
/// </summary>
internal sealed class RangeObservableCollection<T> : ObservableCollection<T>
{
    public void ReplaceAll(IEnumerable<T> items)
    {
        ArgumentNullException.ThrowIfNull(items);
        // Snapshot first so replacing from this collection (or a deferred query over it)
        // cannot be invalidated by the clear below.
        var replacement = items.ToArray();

        CheckReentrancy();
        Items.Clear();
        foreach (var item in replacement)
        {
            Items.Add(item);
        }

        OnPropertyChanged(new PropertyChangedEventArgs(nameof(Count)));
        OnPropertyChanged(new PropertyChangedEventArgs("Item[]"));
        OnCollectionChanged(new NotifyCollectionChangedEventArgs(
            NotifyCollectionChangedAction.Reset
        ));
    }
}
