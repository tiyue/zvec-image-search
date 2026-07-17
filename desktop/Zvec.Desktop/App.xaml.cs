using System.Windows;
using System.Windows.Threading;
using Zvec.Desktop.Services;

namespace Zvec.Desktop;

public partial class App : System.Windows.Application
{
    private const string SingleInstanceMutexName = @"Local\Zvec.ImageSearch.Desktop";
    private const string ActivateInstanceEventName =
        @"Local\Zvec.ImageSearch.Desktop.Activate";
    private const string ExitInstanceEventName =
        @"Local\Zvec.ImageSearch.Desktop.Exit";
    private const string ExitRunningInstanceArgument = "--exit-running-instance";

    private Mutex? _singleInstanceMutex;
    private bool _ownsSingleInstanceMutex;
    private EventWaitHandle? _activateInstanceEvent;
    private EventWaitHandle? _exitInstanceEvent;
    private RegisteredWaitHandle? _activateInstanceWait;
    private RegisteredWaitHandle? _exitInstanceWait;
    private MainWindow? _mainWindow;
    private TrayIconService? _trayIconService;
    private int _isShuttingDown;
    private int _pendingExitRequest;

    private bool IsShuttingDown => Volatile.Read(ref _isShuttingDown) != 0;

    protected override void OnStartup(StartupEventArgs e)
    {
        var requestExistingInstanceExit = e.Args.Any(argument =>
            string.Equals(
                argument,
                ExitRunningInstanceArgument,
                StringComparison.OrdinalIgnoreCase
            )
        );
        _singleInstanceMutex = new Mutex(
            initiallyOwned: true,
            name: SingleInstanceMutexName,
            createdNew: out var createdNew
        );
        if (!createdNew)
        {
            var signalName = requestExistingInstanceExit
                ? ExitInstanceEventName
                : ActivateInstanceEventName;
            var signaled = TrySignalExistingInstance(signalName);
            if (!signaled && !requestExistingInstanceExit)
            {
                MessageBox.Show(
                    "Zvec 图片搜索已经在运行，请从系统托盘恢复窗口。",
                    "Zvec 图片搜索",
                    MessageBoxButton.OK,
                    MessageBoxImage.Information
                );
            }
            Shutdown(signaled ? 0 : 2);
            return;
        }

        _ownsSingleInstanceMutex = true;
        if (requestExistingInstanceExit)
        {
            // This switch is intended for installer smoke tests and automation.
            // Starting a new UI when no existing instance is present would be
            // surprising, so return a distinct non-zero result instead.
            Shutdown(3);
            return;
        }

        DispatcherUnhandledException += OnDispatcherUnhandledException;
        base.OnStartup(e);

        try
        {
            InitializeInstanceSignals();
            _mainWindow = new MainWindow();
            MainWindow = _mainWindow;
            _trayIconService = new TrayIconService(
                Dispatcher,
                RestoreMainWindow,
                RequestApplicationExit
            );
            _mainWindow.HiddenToTray += MainWindow_HiddenToTray;
            _mainWindow.Closed += MainWindow_Closed;
            _mainWindow.Show();
            ProcessPendingExitRequest();
        }
        catch (Exception exception)
        {
            MessageBox.Show(
                exception.Message,
                "Zvec 图片搜索启动失败",
                MessageBoxButton.OK,
                MessageBoxImage.Error
            );
            Shutdown(1);
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        Interlocked.Exchange(ref _isShuttingDown, 1);
        DispatcherUnhandledException -= OnDispatcherUnhandledException;
        if (_mainWindow is not null)
        {
            _mainWindow.HiddenToTray -= MainWindow_HiddenToTray;
            _mainWindow.Closed -= MainWindow_Closed;
        }
        _trayIconService?.Dispose();
        _trayIconService = null;
        _activateInstanceWait?.Unregister(null);
        _exitInstanceWait?.Unregister(null);
        _activateInstanceWait = null;
        _exitInstanceWait = null;
        _activateInstanceEvent?.Dispose();
        _exitInstanceEvent?.Dispose();
        _activateInstanceEvent = null;
        _exitInstanceEvent = null;
        if (_ownsSingleInstanceMutex)
        {
            _singleInstanceMutex?.ReleaseMutex();
        }
        _singleInstanceMutex?.Dispose();
        base.OnExit(e);
    }

    private void InitializeInstanceSignals()
    {
        _activateInstanceEvent = new EventWaitHandle(
            initialState: false,
            EventResetMode.AutoReset,
            ActivateInstanceEventName
        );
        _exitInstanceEvent = new EventWaitHandle(
            initialState: false,
            EventResetMode.AutoReset,
            ExitInstanceEventName
        );
        _activateInstanceWait = ThreadPool.RegisterWaitForSingleObject(
            _activateInstanceEvent,
            (_, timedOut) =>
            {
                if (!timedOut)
                {
                    DispatchToUi(RestoreMainWindow);
                }
            },
            state: null,
            Timeout.Infinite,
            executeOnlyOnce: false
        );
        _exitInstanceWait = ThreadPool.RegisterWaitForSingleObject(
            _exitInstanceEvent,
            (_, timedOut) =>
            {
                if (!timedOut)
                {
                    Interlocked.Exchange(ref _pendingExitRequest, 1);
                    DispatchToUi(ProcessPendingExitRequest);
                }
            },
            state: null,
            Timeout.Infinite,
            executeOnlyOnce: false
        );
    }

    private void DispatchToUi(Action action)
    {
        if (IsShuttingDown || Dispatcher.HasShutdownStarted)
        {
            return;
        }
        try
        {
            _ = Dispatcher.BeginInvoke(DispatcherPriority.Normal, action);
        }
        catch (InvalidOperationException)
        {
            // The dispatcher can begin shutting down between the state check
            // and BeginInvoke. A late activation/exit signal is safe to drop.
        }
    }

    private static bool TrySignalExistingInstance(string eventName)
    {
        var deadline = DateTime.UtcNow.AddSeconds(2);
        do
        {
            try
            {
                using var signal = EventWaitHandle.OpenExisting(eventName);
                return signal.Set();
            }
            catch (WaitHandleCannotBeOpenedException)
            {
                Thread.Sleep(50);
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
        }
        while (DateTime.UtcNow < deadline);
        return false;
    }

    private void MainWindow_HiddenToTray(object? sender, EventArgs e) =>
        _trayIconService?.ShowWindowHiddenNotification();

    private void MainWindow_Closed(object? sender, EventArgs e)
    {
        Interlocked.Exchange(ref _isShuttingDown, 1);
        _trayIconService?.Dispose();
        _trayIconService = null;
        Shutdown();
    }

    private void RestoreMainWindow()
    {
        if (IsShuttingDown || _mainWindow is null)
        {
            return;
        }

        if (!_mainWindow.IsVisible)
        {
            _mainWindow.Show();
        }
        if (_mainWindow.WindowState == WindowState.Minimized)
        {
            _mainWindow.WindowState = WindowState.Normal;
        }
        _mainWindow.Activate();
        _mainWindow.Topmost = true;
        _mainWindow.Topmost = false;
        _mainWindow.Focus();
    }

    private void RequestApplicationExit()
    {
        if (IsShuttingDown || _mainWindow is null)
        {
            return;
        }
        RestoreMainWindow();
        _mainWindow.RequestApplicationExit();
    }

    private void ProcessPendingExitRequest()
    {
        if (_mainWindow is null || IsShuttingDown)
        {
            return;
        }
        if (Interlocked.Exchange(ref _pendingExitRequest, 0) != 0)
        {
            RequestApplicationExit();
        }
    }

    private static void OnDispatcherUnhandledException(
        object sender,
        DispatcherUnhandledExceptionEventArgs e)
    {
        MessageBox.Show(
            e.Exception.Message,
            "桌面程序发生错误",
            MessageBoxButton.OK,
            MessageBoxImage.Error
        );
        e.Handled = true;
    }
}
