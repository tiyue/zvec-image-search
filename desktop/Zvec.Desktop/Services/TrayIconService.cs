using System.Drawing;
using System.Windows.Threading;
using Forms = System.Windows.Forms;

namespace Zvec.Desktop.Services;

/// <summary>
/// Owns the Windows notification-area icon and marshals all callbacks to WPF's
/// dispatcher. The service does not own backend lifetime; it only exposes the
/// user's explicit "open" and "exit" intentions.
/// </summary>
public sealed class TrayIconService : IDisposable
{
    private readonly Dispatcher _dispatcher;
    private readonly Action _openWindow;
    private readonly Action _exitApplication;
    private readonly Icon _icon;
    private readonly Forms.ContextMenuStrip _contextMenu;
    private readonly Forms.NotifyIcon _notifyIcon;
    private bool _hiddenNotificationShown;
    private bool _disposed;

    public TrayIconService(
        Dispatcher dispatcher,
        Action openWindow,
        Action exitApplication)
    {
        _dispatcher = dispatcher ?? throw new ArgumentNullException(nameof(dispatcher));
        _openWindow = openWindow ?? throw new ArgumentNullException(nameof(openWindow));
        _exitApplication = exitApplication
            ?? throw new ArgumentNullException(nameof(exitApplication));

        _icon = LoadApplicationIcon();
        var openItem = new Forms.ToolStripMenuItem("打开主窗口");
        var exitItem = new Forms.ToolStripMenuItem("退出 Zvec");
        openItem.Click += (_, _) => Dispatch(_openWindow);
        exitItem.Click += (_, _) => Dispatch(_exitApplication);

        _contextMenu = new Forms.ContextMenuStrip
        {
            ShowCheckMargin = false,
            ShowImageMargin = false,
        };
        _contextMenu.Items.Add(openItem);
        _contextMenu.Items.Add(new Forms.ToolStripSeparator());
        _contextMenu.Items.Add(exitItem);

        _notifyIcon = new Forms.NotifyIcon
        {
            ContextMenuStrip = _contextMenu,
            Icon = _icon,
            Text = "Zvec 图片语义搜索",
            Visible = true,
        };
        _notifyIcon.MouseClick += NotifyIcon_MouseClick;
    }

    public void ShowWindowHiddenNotification()
    {
        if (_disposed || _hiddenNotificationShown)
        {
            return;
        }
        _hiddenNotificationShown = true;
        try
        {
            _notifyIcon.ShowBalloonTip(
                timeout: 3000,
                tipTitle: "Zvec 已转入后台",
                tipText: "窗口已隐藏，索引和标注任务会继续运行。单击托盘图标即可恢复。",
                tipIcon: Forms.ToolTipIcon.Info
            );
        }
        catch (InvalidOperationException)
        {
            // Windows can reject a balloon while Explorer is restarting. The
            // tray icon remains usable, so this notification is best-effort.
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _notifyIcon.MouseClick -= NotifyIcon_MouseClick;
        _notifyIcon.Visible = false;
        _notifyIcon.Dispose();
        _contextMenu.Dispose();
        _icon.Dispose();
    }

    private void NotifyIcon_MouseClick(object? sender, Forms.MouseEventArgs e)
    {
        if (e.Button == Forms.MouseButtons.Left)
        {
            Dispatch(_openWindow);
        }
    }

    private void Dispatch(Action action)
    {
        if (_disposed || _dispatcher.HasShutdownStarted)
        {
            return;
        }
        if (_dispatcher.CheckAccess())
        {
            action();
            return;
        }
        _ = _dispatcher.BeginInvoke(DispatcherPriority.Normal, action);
    }

    private static Icon LoadApplicationIcon()
    {
        try
        {
            var processPath = Environment.ProcessPath;
            if (!string.IsNullOrWhiteSpace(processPath))
            {
                using var associatedIcon = Icon.ExtractAssociatedIcon(processPath);
                if (associatedIcon is not null)
                {
                    return (Icon)associatedIcon.Clone();
                }
            }
        }
        catch (Exception exception) when (
            exception is ArgumentException or
            IOException or
            UnauthorizedAccessException)
        {
            // Fall back to a guaranteed Windows icon if the executable icon
            // cannot be read from an unusual deployment path.
        }
        return (Icon)SystemIcons.Application.Clone();
    }
}
