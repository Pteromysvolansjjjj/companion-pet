using System;
using System.Collections.Generic;
using System.Collections;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Forms = System.Windows.Forms;
using Drawing = System.Drawing;

namespace LolCompanionDesktop
{
    internal static class Program
    {
        internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024 };
        internal static string Root;
        internal static bool Smoke;
        internal static bool Rendering;
        private static readonly object OutputLock = new object();

        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                // A hidden WinExe has redirected handles but no console codepage.
                // Use UTF-8 streams directly instead of SetConsoleOutputCP.
                Console.SetIn(new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false, true), true));
                Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true });
                Console.SetError(new StreamWriter(Console.OpenStandardError(), new UTF8Encoding(false)) { AutoFlush = true });
                Root = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
                string renderPath = null;
                for (int i = 0; i < args.Length; i++)
                {
                    if (args[i] == "--root" && i + 1 < args.Length) Root = Path.GetFullPath(args[++i]);
                    else if (args[i] == "--render-test" && i + 1 < args.Length) renderPath = Path.GetFullPath(args[++i]);
                    else if (args[i] == "--smoke-test") Smoke = true;
                    else throw new ArgumentException("Unknown or incomplete command-line argument.");
                }
                Rendering = renderPath != null;
                Application app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
                CompanionWindow companion = new CompanionWindow(Root, Smoke || Rendering);
                if (Rendering)
                {
                    companion.RenderTest(renderPath);
                    return 0;
                }
                app.Startup += delegate
                {
                    if (!Smoke) companion.ShowPet();
                    Emit(new { type = "ready", protocolVersion = 3, mode = Smoke ? "smoke" : "desktop" });
                    Thread input = new Thread(delegate()
                    {
                        try
                        {
                            string line;
                            while ((line = Console.ReadLine()) != null)
                            {
                                string captured = line;
                                if (!app.Dispatcher.HasShutdownStarted)
                                    app.Dispatcher.BeginInvoke(new Action(delegate { companion.Receive(captured); }));
                            }
                            if (!app.Dispatcher.HasShutdownStarted)
                                app.Dispatcher.BeginInvoke(new Action(delegate { companion.ExitPet("stdin-closed"); }));
                        }
                        catch (Exception ex)
                        {
                            Error("stdin", ex);
                            if (!app.Dispatcher.HasShutdownStarted)
                                app.Dispatcher.BeginInvoke(new Action(delegate { companion.ExitPet("stdin-error"); }));
                        }
                    });
                    input.IsBackground = true;
                    input.Name = "companion-stdin";
                    input.Start();
                };
                app.Exit += delegate { companion.DisposeResources(); };
                app.DispatcherUnhandledException += delegate(object sender, System.Windows.Threading.DispatcherUnhandledExceptionEventArgs e)
                {
                    Error("dispatcher", e.Exception);
                    e.Handled = true;
                    companion.ExitPet("error");
                };
                app.Run();
                return 0;
            }
            catch (Exception ex)
            {
                Error("startup", ex);
                return 1;
            }
        }

        internal static void Emit(object value)
        {
            lock (OutputLock)
            {
                try { Console.Out.WriteLine(Json.Serialize(value)); Console.Out.Flush(); }
                catch (IOException) { }
            }
        }

        internal static void Error(string operation, Exception ex)
        {
            // Do not put reply text, credentials or session identifiers in diagnostics.
            try { Console.Error.WriteLine("desktop: " + operation + " failed (" + ex.GetType().Name + ")"); }
            catch (IOException) { }
        }
    }

    public sealed class DesktopSettings
    {
        public int Version = 1;
        public double Width = 420;
        public double FontSize = 16;
        public double? Left;
        public double? Top;
        public bool SnapToEdges = false;
    }

    // Host-owned conversation preferences. The desktop only edits a request;
    // acknowledged values remain authoritative and are never saved locally.
    internal sealed class ConversationPreferences
    {
        internal int DecisionIntervalMs = 15000;
        internal int PollIntervalMs = 1000;
        internal int BubbleHoldMs = 5000;
        internal long ReplyLength = 20;
        internal string DataMode = "basic";

        internal static ConversationPreferences Parse(Dictionary<string, object> values)
        {
            if (values == null) throw new InvalidReplyException("INVALID_PREFERENCES");
            object dataMode;
            if (!values.TryGetValue("dataMode", out dataMode) || !(dataMode is string) ||
                ((string)dataMode != "basic" && (string)dataMode != "detailed"))
                throw new InvalidReplyException("INVALID_PREFERENCES");
            return new ConversationPreferences
            {
                DecisionIntervalMs = (int)Integer(values, "decisionIntervalMs", 500, Int32.MaxValue),
                PollIntervalMs = (int)Integer(values, "pollIntervalMs", 250, Int32.MaxValue),
                BubbleHoldMs = (int)Integer(values, "bubbleHoldMs", 500, 30000),
                ReplyLength = Integer(values, "replyLength", 0, 9007199254740991L),
                DataMode = (string)dataMode
            };
        }

        private static long Integer(Dictionary<string, object> values, string name, long minimum, long maximum)
        {
            object value;
            if (!values.TryGetValue(name, out value) ||
                (!(value is int) && !(value is long) && !(value is decimal) && !(value is double)))
                throw new InvalidReplyException("INVALID_PREFERENCES");
            double numeric = Convert.ToDouble(value, CultureInfo.InvariantCulture);
            if (Double.IsNaN(numeric) || Double.IsInfinity(numeric) || numeric != Math.Floor(numeric) || numeric < minimum || numeric > maximum)
                throw new InvalidReplyException("INVALID_PREFERENCES");
            return (long)numeric;
        }
    }

    internal sealed class InvalidReplyException : FormatException
    {
        internal readonly string Code;
        internal InvalidReplyException(string code) { Code = code; }
    }

    // A display timeline, not a model/personality rule. Its monotonic
    // time input makes animation and cancellation independently testable.
    internal sealed class BubblePlayback
    {
        internal const double FadeMs = 250;
        internal const int DefaultHoldMs = 5000;
        internal int HoldMs { get; private set; }
        internal double SegmentMs { get { return FadeMs + HoldMs + FadeMs; } }
        private string[] lines = new string[0];
        internal string Text { get; private set; }
        internal string Phase { get; private set; }
        internal double Opacity { get; private set; }
        internal int Remaining { get; private set; }
        internal bool Active { get { return lines.Length > 0; } }
        internal BubblePlayback() { Clear(); }
        internal void Start(string[] value, int holdMs = DefaultHoldMs)
        {
            HoldMs = holdMs;
            lines = (string[])value.Clone();
            Advance(0);
        }
        internal void Clear() { lines = new string[0]; Text = ""; Phase = "idle"; Opacity = 0; Remaining = 0; }
        internal void Advance(double elapsedMs)
        {
            if (!Active) return;
            double pageIndex = Math.Floor(Math.Max(0, elapsedMs) / SegmentMs);
            if (pageIndex >= lines.Length) { Clear(); return; }
            int index = (int)pageIndex;
            double local = Math.Max(0, elapsedMs) - index * SegmentMs;
            Text = lines[index];
            Remaining = lines.Length - index - 1;
            if (local < FadeMs) { Phase = "fade-in"; Opacity = local / FadeMs; }
            else if (local < FadeMs + HoldMs) { Phase = "hold"; Opacity = 1; }
            else { Phase = "fade-out"; Opacity = 1 - (local - FadeMs - HoldMs) / FadeMs; }
        }
    }

    internal sealed partial class CompanionWindow : Window
    {
        private const double DesignSize = 1026;
        private readonly string settingsPath;
        private readonly bool headless;
        private readonly DesktopSettings settings;
        private readonly BitmapSource picture;
        private readonly byte[] alphaPixels;
        private readonly int alphaStride;
        private readonly Viewbox artwork;
        private readonly Canvas canvas;
        private readonly Canvas bubbleLayer;
        private readonly System.Windows.Shapes.Path bubbleShape;
        private readonly TextBlock bubbleText;
        private readonly Image characterImage;
        private readonly BubblePlayback playback = new BubblePlayback();
        private readonly Stopwatch playbackClock = new Stopwatch();
        private readonly DispatcherTimer playbackTimer;
        private bool reportedPlaybackActive;
        private Action<bool> playbackObserver;
        private string currentCommandId;
        private Geometry bubbleGeometry;
        private bool userHidden;
        private readonly Forms.NotifyIcon tray;
        private HwndSource nativeSource;
        private bool exiting;
        private bool resourcesDisposed;
        private bool dragging;
        private Point dragStart;
        private double dragLeft;
        private double dragTop;
        private string reply = "";
        private string status = "等待原会话回复";
        private Window settingsWindow;
        private string preferenceBindingId;
        private string preferencePendingRequestId;
        private ConversationPreferences conversationPreferences = new ConversationPreferences();
        private StackPanel preferenceControls;
        private ComboBox preferenceInterval;
        private ComboBox preferenceDataMode;
        private TextBox preferenceHold;
        private TextBox preferenceLength;
        private TextBlock preferenceSampling;
        private TextBlock preferenceMessage;
        private Button preferenceApply;
        private Action<object> preferenceRequestObserver;
        private double dpiScale = 1;

        internal CompanionWindow(string root, bool noNativeWindow)
        {
            headless = noNativeWindow;
            settingsPath = Path.Combine(root, "data", "desktop-settings.json");
            settings = LoadSettings();
            Title = "LOL 陪玩桌宠";
            Width = Height = settings.Width;
            WindowStyle = WindowStyle.None;
            ResizeMode = ResizeMode.NoResize;
            AllowsTransparency = true;
            Background = Brushes.Transparent;
            Topmost = true;
            ShowInTaskbar = false;
            ShowActivated = false;
            Focusable = false;
            UseLayoutRounding = true;
            SnapsToDevicePixels = true;

            BitmapImage bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.UriSource = new Uri(Path.Combine(root, "assets", "DSniang02-character.png"), UriKind.Absolute);
            bitmap.EndInit();
            bitmap.Freeze();
            picture = bitmap;
            FormatConvertedBitmap alphaBitmap = new FormatConvertedBitmap(bitmap, PixelFormats.Bgra32, null, 0);
            alphaStride = alphaBitmap.PixelWidth * 4;
            alphaPixels = new byte[alphaStride * alphaBitmap.PixelHeight];
            alphaBitmap.CopyPixels(alphaPixels, alphaStride, 0);

            canvas = new Canvas { Width = DesignSize, Height = DesignSize };
            characterImage = new Image { Source = picture, Width = DesignSize, Height = DesignSize, Stretch = Stretch.Fill };
            RenderOptions.SetBitmapScalingMode(characterImage, BitmapScalingMode.HighQuality);
            canvas.Children.Add(characterImage);
            bubbleText = new TextBlock
            {
                Foreground = new SolidColorBrush(Color.FromRgb(36, 44, 84)),
                FontFamily = new FontFamily("Microsoft YaHei UI, Microsoft YaHei, Segoe UI"),
                TextWrapping = TextWrapping.Wrap,
                Focusable = false
            };
            bubbleLayer = new Canvas { Width = 740, Visibility = Visibility.Collapsed, Opacity = 0, IsHitTestVisible = false };
            bubbleShape = new System.Windows.Shapes.Path
            {
                Fill = new SolidColorBrush(Color.FromRgb(255, 254, 255)),
                Stroke = new SolidColorBrush(Color.FromRgb(36, 44, 84)),
                StrokeThickness = 4,
                StrokeLineJoin = PenLineJoin.Round
            };
            bubbleLayer.Children.Add(bubbleShape);
            Canvas.SetLeft(bubbleText, 30);
            Canvas.SetTop(bubbleText, 24);
            bubbleLayer.Children.Add(bubbleText);
            Canvas.SetLeft(bubbleLayer, 64);
            canvas.Children.Add(bubbleLayer);
            playbackTimer = new DispatcherTimer(DispatcherPriority.Render, Dispatcher) { Interval = TimeSpan.FromMilliseconds(16) };
            playbackTimer.Tick += delegate { playback.Advance(playbackClock.Elapsed.TotalMilliseconds); ApplyPlayback(); };
            artwork = new Viewbox { Stretch = Stretch.Uniform, Child = canvas, Focusable = false };
            Content = artwork;
            UpdateTextSize();
            ContextMenu = BuildContextMenu();
            ToolTip = "三击角色聊天；拖动角色或气泡移动；Ctrl + 滚轮缩放；右键设置";
            PreviewMouseLeftButtonDown += BeginDrag;
            PreviewMouseMove += ContinueDrag;
            PreviewMouseLeftButtonUp += EndDrag;
            LostMouseCapture += delegate { if (dragging) clickGesture.Cancel(); dragging = false; };
            LocationChanged += delegate { PositionChatInput(); };
            SizeChanged += delegate { PositionChatInput(); };
            PreviewMouseWheel += OnWheel;
            SourceInitialized += OnSourceInitialized;
            Closing += delegate { if (!exiting) ExitPet("window-closed"); };

            if (!headless)
            {
                using (Drawing.Graphics graphics = Drawing.Graphics.FromHwnd(IntPtr.Zero)) dpiScale = graphics.DpiX / 96.0;
                tray = new Forms.NotifyIcon();
                tray.Icon = Drawing.SystemIcons.Application;
                tray.Text = "LOL 陪玩桌宠";
                Forms.ContextMenuStrip menu = new Forms.ContextMenuStrip();
                menu.Items.Add("显示桌宠", null, delegate { Dispatcher.BeginInvoke(new Action(ShowPet)); });
                menu.Items.Add("隐藏桌宠", null, delegate { Dispatcher.BeginInvoke(new Action(HidePet)); });
                menu.Items.Add("和她聊天…", null, delegate { Dispatcher.BeginInvoke(new Action(OpenChatInput)); });
                menu.Items.Add("设置", null, delegate { Dispatcher.BeginInvoke(new Action(OpenSettings)); });
                menu.Items.Add("恢复位置", null, delegate { Dispatcher.BeginInvoke(new Action(delegate { ResetPosition(); ShowPet(); })); });
                menu.Items.Add(new Forms.ToolStripSeparator());
                menu.Items.Add("退出", null, delegate { Dispatcher.BeginInvoke(new Action(delegate { ExitPet("tray-exit"); })); });
                tray.ContextMenuStrip = menu;
                tray.DoubleClick += delegate { Dispatcher.BeginInvoke(new Action(ShowPet)); };
                tray.Visible = true;
            }
            RestorePosition();
        }

        private DesktopSettings LoadSettings()
        {
            try
            {
                if (File.Exists(settingsPath))
                {
                    DesktopSettings saved = Program.Json.Deserialize<DesktopSettings>(File.ReadAllText(settingsPath, Encoding.UTF8));
                    if (saved != null)
                    {
                        saved.Width = Clamp(saved.Width, 240, 640, 420);
                        saved.FontSize = Clamp(saved.FontSize, 10, 28, 16);
                        if (saved.Left.HasValue && !IsFinite(saved.Left.Value)) saved.Left = null;
                        if (saved.Top.HasValue && !IsFinite(saved.Top.Value)) saved.Top = null;
                        return saved;
                    }
                }
            }
            catch (Exception ex) { Program.Error("load-settings", ex); }
            return new DesktopSettings();
        }

        private void SaveSettings()
        {
            if (headless) return;
            try
            {
                settings.Width = Width;
                settings.Left = Left;
                settings.Top = Top;
                Directory.CreateDirectory(Path.GetDirectoryName(settingsPath));
                string temporary = settingsPath + ".tmp";
                File.WriteAllText(temporary, Program.Json.Serialize(settings), new UTF8Encoding(false));
                if (File.Exists(settingsPath)) File.Replace(temporary, settingsPath, null);
                else File.Move(temporary, settingsPath);
            }
            catch (Exception ex) { Program.Error("save-settings", ex); }
        }

        internal void ShowPet()
        {
            userHidden = false;
            if (exiting || headless) return;
            EnsureVisiblePosition();
            Show();
            // ShowActivated=false and WS_EX_NOACTIVATE preserve the game's focus.
            if (nativeSource != null)
                SetWindowPos(nativeSource.Handle, new IntPtr(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010);
        }

        private void HidePet()
        {
            userHidden = true;
            DismissChatInput(false);
            ClearBubble(true);
            if (!headless) Hide();
        }

        private void OnSourceInitialized(object sender, EventArgs e)
        {
            nativeSource = HwndSource.FromHwnd(new WindowInteropHelper(this).Handle);
            IntPtr handle = nativeSource.Handle;
            long style = GetWindowLongPtr(handle, -20).ToInt64();
            style = (style | 0x08000000L | 0x00000080L) & ~0x00040000L;
            SetWindowLongPtr(handle, -20, new IntPtr(style));
            nativeSource.AddHook(WindowMessage);
            if (nativeSource.CompositionTarget != null) dpiScale = nativeSource.CompositionTarget.TransformToDevice.M11;
            EnsureVisiblePosition();
        }

        private IntPtr WindowMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            if (message == 0x0021) // WM_MOUSEACTIVATE: process clicks without activating this window.
            {
                handled = true;
                return new IntPtr(3);
            }
            if (message == 0x0084) // WM_NCHITTEST: transparent picture pixels do not block the game.
            {
                long xy = lParam.ToInt64();
                Point point = PointFromScreen(new Point((short)(xy & 0xffff), (short)((xy >> 16) & 0xffff)));
                Point designPoint = new Point(point.X / Math.Max(1, ActualWidth) * DesignSize, point.Y / Math.Max(1, ActualHeight) * DesignSize);
                if (!IsInteractiveAt(designPoint))
                {
                    handled = true;
                    return new IntPtr(-1);
                }
            }
            return IntPtr.Zero;
        }

        private bool IsInteractiveAt(Point point)
        {
            if (IsCharacterAt(point)) return true;
            return bubbleLayer.Visibility == Visibility.Visible && bubbleLayer.Opacity > 0.01 && bubbleGeometry != null &&
                bubbleGeometry.FillContains(new Point(point.X - Canvas.GetLeft(bubbleLayer), point.Y - Canvas.GetTop(bubbleLayer)));
        }

        private bool IsCharacterAt(Point point)
        {
            int x = (int)(point.X / DesignSize * picture.PixelWidth);
            int y = (int)(point.Y / DesignSize * picture.PixelHeight);
            return x >= 0 && y >= 0 && x < picture.PixelWidth && y < picture.PixelHeight && alphaPixels[y * alphaStride + x * 4 + 3] >= 12;
        }

        private ContextMenu BuildContextMenu()
        {
            ContextMenu menu = new ContextMenu();
            AddMenu(menu, "和她聊天…（三击角色）", OpenChatInput);
            AddMenu(menu, "设置…", OpenSettings);
            AddMenu(menu, "放大", delegate { ResizePet(Width + 40); });
            AddMenu(menu, "缩小", delegate { ResizePet(Width - 40); });
            AddMenu(menu, "隐藏（托盘可找回）", HidePet);
            AddMenu(menu, "恢复位置", ResetPosition);
            menu.Items.Add(new Separator());
            AddMenu(menu, "退出", delegate { ExitPet("menu-exit"); });
            return menu;
        }

        private static void AddMenu(ContextMenu menu, string title, Action action)
        {
            MenuItem item = new MenuItem { Header = title };
            item.Click += delegate { action(); };
            menu.Items.Add(item);
        }

        private void BeginDrag(object sender, MouseButtonEventArgs e)
        {
            DependencyObject origin = e.OriginalSource as DependencyObject;
            for (DependencyObject current = origin; current != null; current = ParentOf(current))
                if (current is ScrollBar || current is Thumb || current is RepeatButton) return;
            NativePoint point;
            if (!GetCursorPos(out point)) return;
            dragStart = new Point(point.X, point.Y);
            dragLeft = Left;
            dragTop = Top;
            Point local = e.GetPosition(this);
            clickGesture.Down(new Point(point.X / dpiScale, point.Y / dpiScale),
                IsCharacterAt(new Point(local.X / Math.Max(1, ActualWidth) * DesignSize, local.Y / Math.Max(1, ActualHeight) * DesignSize)));
            dragging = CaptureMouse();
            if (!dragging) clickGesture.Cancel();
            e.Handled = dragging;
        }

        private void ContinueDrag(object sender, MouseEventArgs e)
        {
            if (!dragging || e.LeftButton != MouseButtonState.Pressed) return;
            NativePoint point;
            if (!GetCursorPos(out point)) return;
            if (!clickGesture.Move(new Point(point.X / dpiScale, point.Y / dpiScale))) return;
            Left = dragLeft + (point.X - dragStart.X) / dpiScale;
            Top = dragTop + (point.Y - dragStart.Y) / dpiScale;
            e.Handled = true;
        }

        private void EndDrag(object sender, MouseButtonEventArgs e)
        {
            if (!dragging) return;
            dragging = false;
            ReleaseMouseCapture();
            NativePoint point;
            bool openChat = GetCursorPos(out point) && clickGesture.Up(new Point(point.X / dpiScale, point.Y / dpiScale), Environment.TickCount);
            if (settings.SnapToEdges) SnapPosition();
            EnsureVisiblePosition();
            SaveSettings();
            if (openChat) OpenChatInput();
            e.Handled = true;
        }

        private void OnWheel(object sender, MouseWheelEventArgs e)
        {
            if ((Keyboard.Modifiers & ModifierKeys.Control) != 0) ResizePet(Width + (e.Delta > 0 ? 20 : -20));
            e.Handled = true;
        }

        private void ResizePet(double width)
        {
            double oldWidth = Width;
            Width = Height = Clamp(width, 240, 640, 420);
            Left += oldWidth - Width;
            Top += oldWidth - Width;
            UpdateTextSize();
            EnsureVisiblePosition();
            SaveSettings();
        }

        private void UpdateTextSize()
        {
            bubbleText.FontSize = settings.FontSize * DesignSize / Math.Max(240, Width);
            bubbleText.LineHeight = bubbleText.FontSize * 1.4;
            bubbleText.Width = 680;
            bubbleText.Measure(new Size(680, Double.PositiveInfinity));
            double minimum = 10 * DesignSize / Math.Max(240, Width);
            // The saved font is a preference; fit the whole validated short line
            // at small pet sizes without scrolling, ellipsis or text truncation.
            while (bubbleText.DesiredSize.Height > 250 && bubbleText.FontSize > minimum)
            {
                bubbleText.FontSize = Math.Max(minimum, bubbleText.FontSize - 1);
                bubbleText.LineHeight = bubbleText.FontSize * 1.4;
                bubbleText.Measure(new Size(680, Double.PositiveInfinity));
            }
            double bodyHeight = Math.Max(94, bubbleText.DesiredSize.Height + 48);
            StreamGeometry geometry = new StreamGeometry();
            using (StreamGeometryContext drawing = geometry.Open())
            {
                drawing.BeginFigure(new Point(24, 0), true, true);
                drawing.LineTo(new Point(716, 0), true, false);
                drawing.QuadraticBezierTo(new Point(740, 0), new Point(740, 24), true, false);
                drawing.LineTo(new Point(740, bodyHeight - 24), true, false);
                drawing.QuadraticBezierTo(new Point(740, bodyHeight), new Point(716, bodyHeight), true, false);
                drawing.LineTo(new Point(675, bodyHeight), true, false);
                drawing.LineTo(new Point(693, bodyHeight + 32), true, false);
                drawing.LineTo(new Point(640, bodyHeight), true, false);
                drawing.LineTo(new Point(24, bodyHeight), true, false);
                drawing.QuadraticBezierTo(new Point(0, bodyHeight), new Point(0, bodyHeight - 24), true, false);
                drawing.LineTo(new Point(0, 24), true, false);
                drawing.QuadraticBezierTo(new Point(0, 0), new Point(24, 0), true, false);
            }
            geometry.Freeze();
            bubbleGeometry = geometry;
            bubbleShape.Data = geometry;
            bubbleLayer.Height = bodyHeight + 34;
            Canvas.SetTop(bubbleLayer, 410 - bubbleLayer.Height);
        }

        private Rect WorkAreaForWindow()
        {
            int x = (int)((Left + Width / 2) * dpiScale);
            int y = (int)((Top + Height / 2) * dpiScale);
            Drawing.Rectangle area = Forms.Screen.FromPoint(new Drawing.Point(x, y)).WorkingArea;
            return new Rect(area.Left / dpiScale, area.Top / dpiScale, area.Width / dpiScale, area.Height / dpiScale);
        }

        private void RestorePosition()
        {
            if (headless) { Left = 0; Top = 0; return; }
            if (settings.Left.HasValue && settings.Top.HasValue)
            {
                Left = settings.Left.Value;
                Top = settings.Top.Value;
                EnsureVisiblePosition();
            }
            else ResetPosition();
        }

        private void ResetPosition()
        {
            if (headless) { Left = 0; Top = 0; return; }
            Drawing.Rectangle area = Forms.Screen.PrimaryScreen.WorkingArea;
            Left = area.Right / dpiScale - Width - 16;
            Top = area.Bottom / dpiScale - Height - 16;
            EnsureVisiblePosition();
            SaveSettings();
        }

        private void EnsureVisiblePosition()
        {
            if (headless) return;
            if (!IsFinite(Left) || !IsFinite(Top)) { ResetPosition(); return; }
            Rect area = WorkAreaForWindow();
            Left = Math.Max(area.Left, Math.Min(Left, Math.Max(area.Left, area.Right - Width)));
            Top = Math.Max(area.Top, Math.Min(Top, Math.Max(area.Top, area.Bottom - Height)));
        }

        private void SnapPosition()
        {
            Rect area = WorkAreaForWindow();
            const double distance = 24;
            if (Math.Abs(Left - area.Left) < distance) Left = area.Left;
            if (Math.Abs(Top - area.Top) < distance) Top = area.Top;
            if (Math.Abs(Left + Width - area.Right) < distance) Left = area.Right - Width;
            if (Math.Abs(Top + Height - area.Bottom) < distance) Top = area.Bottom - Height;
        }

        private void OpenSettings()
        {
            if (headless || exiting) return;
            if (settingsWindow != null) { settingsWindow.Activate(); return; }
            Window dialog = new Window
            {
                Title = "桌宠设置",
                Width = 420,
                SizeToContent = SizeToContent.Height,
                MaxHeight = Math.Max(240, SystemParameters.WorkArea.Height - 40),
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterScreen,
                Topmost = true,
                Background = Brushes.White,
                FontFamily = new FontFamily("Microsoft YaHei UI"),
                FontSize = 14
            };
            StackPanel panel = new StackPanel { Margin = new Thickness(22) };
            panel.Children.Add(new TextBlock { Text = "桌宠大小", Margin = new Thickness(0, 0, 0, 6) });
            Slider size = new Slider { Minimum = 240, Maximum = 640, Value = Width, TickFrequency = 20, IsSnapToTickEnabled = true };
            size.ValueChanged += delegate { ResizePet(size.Value); };
            panel.Children.Add(size);
            panel.Children.Add(new TextBlock { Text = "文字字号", Margin = new Thickness(0, 18, 0, 6) });
            Slider font = new Slider { Minimum = 10, Maximum = 28, Value = settings.FontSize, TickFrequency = 1, IsSnapToTickEnabled = true };
            font.ValueChanged += delegate { settings.FontSize = font.Value; UpdateTextSize(); SaveSettings(); };
            panel.Children.Add(font);
            CheckBox snap = new CheckBox { Content = "靠近屏幕边缘时吸附", IsChecked = settings.SnapToEdges, Margin = new Thickness(0, 18, 0, 8) };
            snap.Checked += delegate { settings.SnapToEdges = true; SaveSettings(); };
            snap.Unchecked += delegate { settings.SnapToEdges = false; SaveSettings(); };
            panel.Children.Add(snap);
            panel.Children.Add(new TextBlock { Text = "拖动角色或气泡可移动。\nCtrl + 滚轮缩放；短句显示后自动淡出。\n字号会按气泡空间适当缩小以完整显示。\n隐藏后可从系统托盘找回。", TextWrapping = TextWrapping.Wrap, Foreground = Brushes.DimGray, Margin = new Thickness(0, 8, 0, 16) });
            panel.Children.Add(BuildConversationSettings());
            panel.Children.Add(new TextBlock { Text = status, TextWrapping = TextWrapping.Wrap, Foreground = Brushes.DimGray, Margin = new Thickness(0, 0, 0, 16) });
            Button reset = new Button { Content = "恢复位置", Padding = new Thickness(8), Margin = new Thickness(0, 0, 0, 8) };
            reset.Click += delegate { ResetPosition(); ShowPet(); };
            panel.Children.Add(reset);
            Button close = new Button { Content = "完成", Padding = new Thickness(8), IsDefault = true };
            close.Click += delegate { dialog.Close(); };
            panel.Children.Add(close);
            dialog.Content = new ScrollViewer
            {
                Content = panel, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                MaxHeight = Math.Max(180, dialog.MaxHeight - 48)
            };
            dialog.Closed += delegate { settingsWindow = null; preferenceControls = null; SaveSettings(); };
            settingsWindow = dialog;
            dialog.Show(); // Only an explicit settings action opens an activating window.
        }

        private StackPanel BuildConversationSettings()
        {
            StackPanel section = new StackPanel { Margin = new Thickness(0, 8, 0, 18) };
            section.Children.Add(new Separator { Margin = new Thickness(0, 0, 0, 14) });
            section.Children.Add(new TextBlock { Text = "当前对话的陪玩设置", FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 12) });
            preferenceControls = new StackPanel();
            section.Children.Add(preferenceControls);
            AddPreferenceLabel("数据量模式");
            preferenceDataMode = new ComboBox { Margin = new Thickness(0, 0, 0, 6) };
            preferenceDataMode.Items.Add(new ComboBoxItem { Content = "主要数据", Tag = "basic" });
            preferenceDataMode.Items.Add(new ComboBoxItem { Content = "详细数据", Tag = "detailed" });
            preferenceControls.Children.Add(preferenceDataMode);
            preferenceControls.Children.Add(new TextBlock
            {
                Text = "主要：阶段、模式、选人、英雄、等级、KDA、存活状态及游戏事件。\n详细：再包含血量、金币、装备等接口提供的状态字段。\n只保留最近的状态变化，省略时会标明；新增事件单独保留。",
                TextWrapping = TextWrapping.Wrap, Foreground = Brushes.DimGray, FontSize = 12, Margin = new Thickness(0, 0, 0, 12)
            });
            AddPreferenceLabel("自动投递间隔（秒，可自定义，最低 0.5）");
            preferenceInterval = new ComboBox { IsEditable = true, IsTextSearchEnabled = false, Margin = new Thickness(0, 0, 0, 6) };
            foreach (string value in new string[] { "15", "10", "5", "2", "1", "0.5" }) preferenceInterval.Items.Add(value);
            preferenceControls.Children.Add(preferenceInterval);
            preferenceSampling = new TextBlock { Foreground = Brushes.DimGray, FontSize = 12, Margin = new Thickness(0, 0, 0, 12) };
            preferenceControls.Children.Add(preferenceSampling);
            AddPreferenceLabel("每页气泡停留（秒，0.5–30）");
            preferenceHold = new TextBox { Margin = new Thickness(0, 0, 0, 12) };
            preferenceControls.Children.Add(preferenceHold);
            AddPreferenceLabel("建议回复总字数（0 表示不限，不是硬上限）");
            preferenceLength = new TextBox { Margin = new Thickness(0, 0, 0, 12) };
            preferenceControls.Children.Add(preferenceLength);
            preferenceApply = new Button { Content = "应用到当前对话", Padding = new Thickness(8), Margin = new Thickness(0, 0, 0, 8) };
            preferenceApply.Click += delegate { ApplyConversationPreferences(); };
            preferenceControls.Children.Add(preferenceApply);
            preferenceMessage = new TextBlock { TextWrapping = TextWrapping.Wrap, Foreground = Brushes.DimGray, Margin = new Thickness(0, 0, 0, 8) };
            section.Children.Add(preferenceMessage);
            section.Children.Add(new TextBlock
            {
                Text = "间隔不是回复耗时，模型忙时合并等待。上一条显示完后，结合最新数据决定下一句，不逐条补播。长回复会完整分页显示。",
                TextWrapping = TextWrapping.Wrap, Foreground = Brushes.DimGray, FontSize = 12
            });
            RefreshConversationSettings(null);
            return section;
        }

        private void AddPreferenceLabel(string text)
        {
            preferenceControls.Children.Add(new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 6) });
        }

        private static string Seconds(int milliseconds)
        {
            return ((decimal)milliseconds / 1000).ToString("0.###", CultureInfo.CurrentCulture);
        }

        private void RefreshConversationSettings(string message)
        {
            if (preferenceControls == null) return;
            foreach (ComboBoxItem item in preferenceDataMode.Items)
                if ((string)item.Tag == conversationPreferences.DataMode) preferenceDataMode.SelectedItem = item;
            preferenceInterval.Text = Seconds(conversationPreferences.DecisionIntervalMs);
            preferenceHold.Text = Seconds(conversationPreferences.BubbleHoldMs);
            preferenceLength.Text = conversationPreferences.ReplyLength.ToString(CultureInfo.InvariantCulture);
            preferenceSampling.Text = "实际采样间隔：" + Seconds(conversationPreferences.PollIntervalMs) + " 秒";
            preferenceControls.IsEnabled = preferenceBindingId != null && preferencePendingRequestId == null;
            preferenceMessage.Foreground = Brushes.DimGray;
            preferenceMessage.Text = preferenceBindingId == null ? "未绑定对话。先在对话中开启陪玩监听。" :
                preferencePendingRequestId != null ? "正在等待当前对话确认设置…" : message ?? "修改后点击应用；也可以在对话中让她调整。";
        }

        private void ReceivePreferences(Dictionary<string, object> message)
        {
            object rawBinding, rawValues, rawRequest, rawError;
            if (!message.TryGetValue("bindingId", out rawBinding) || (rawBinding != null && !(rawBinding is string)))
                throw new InvalidReplyException("INVALID_PREFERENCES");
            string bindingId = rawBinding as string;
            if (bindingId != null && String.IsNullOrWhiteSpace(bindingId)) throw new InvalidReplyException("INVALID_PREFERENCES");
            message.TryGetValue("requestId", out rawRequest);
            message.TryGetValue("error", out rawError);
            if ((rawRequest != null && !(rawRequest is string)) || (rawError != null && !(rawError is string)))
                throw new InvalidReplyException("INVALID_PREFERENCES");
            string requestId = rawRequest as string;
            // An old acknowledgement must never switch the open settings panel
            // back to the conversation that has since been unbound.
            if (requestId != null && (requestId != preferencePendingRequestId || bindingId != preferenceBindingId)) return;
            message.TryGetValue("values", out rawValues);
            ConversationPreferences values = bindingId == null && rawValues == null ? new ConversationPreferences() :
                ConversationPreferences.Parse(rawValues as Dictionary<string, object>);
            bool changedBinding = bindingId != preferenceBindingId;
            if (changedBinding) DismissChatInput(true);
            preferenceBindingId = bindingId;
            conversationPreferences = values;
            if (changedBinding || requestId != null) preferencePendingRequestId = null;
            string error = rawError as string;
            RefreshConversationSettings(error != null ? "设置未保存：" + error : requestId != null ? "已应用到当前对话。" : null);
            if (error != null && preferenceMessage != null) preferenceMessage.Foreground = Brushes.Firebrick;
        }

        private static int ParseSeconds(string value, int minimum, int maximum)
        {
            decimal seconds;
            const NumberStyles style = NumberStyles.AllowDecimalPoint | NumberStyles.AllowLeadingWhite | NumberStyles.AllowTrailingWhite;
            if (!Decimal.TryParse(value, style, CultureInfo.CurrentCulture, out seconds) &&
                !Decimal.TryParse(value, style, CultureInfo.InvariantCulture, out seconds)) throw new FormatException();
            if (seconds < (decimal)minimum / 1000 || seconds > (decimal)maximum / 1000) throw new FormatException();
            decimal milliseconds = seconds * 1000;
            if (milliseconds != Decimal.Truncate(milliseconds)) throw new FormatException();
            return (int)milliseconds;
        }

        private void ApplyConversationPreferences()
        {
            if (preferenceBindingId == null || preferencePendingRequestId != null || preferenceControls == null) return;
            try
            {
                int interval = ParseSeconds(preferenceInterval.Text, 500, Int32.MaxValue);
                int hold = ParseSeconds(preferenceHold.Text, 500, 30000);
                long length;
                if (!Int64.TryParse(preferenceLength.Text.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out length) ||
                    length < 0 || length > 9007199254740991L) throw new FormatException();
                ComboBoxItem selected = preferenceDataMode.SelectedItem as ComboBoxItem;
                if (selected == null) throw new FormatException();
                string mode = (string)selected.Tag;
                Dictionary<string, object> changes = new Dictionary<string, object>();
                if (interval != conversationPreferences.DecisionIntervalMs) changes["decisionIntervalMs"] = interval;
                if (hold != conversationPreferences.BubbleHoldMs) changes["bubbleHoldMs"] = hold;
                if (length != conversationPreferences.ReplyLength) changes["replyLength"] = length;
                if (mode != conversationPreferences.DataMode) changes["dataMode"] = mode;
                if (changes.Count == 0) { preferenceMessage.Text = "设置未改变。"; return; }
                preferencePendingRequestId = Guid.NewGuid().ToString("N");
                object request = new { type = "preferences:set", bindingId = preferenceBindingId, requestId = preferencePendingRequestId, changes = changes };
                preferenceControls.IsEnabled = false;
                preferenceMessage.Foreground = Brushes.DimGray;
                preferenceMessage.Text = "正在等待当前对话确认设置…";
                if (preferenceRequestObserver != null) preferenceRequestObserver(request);
                if (!Program.Rendering) Program.Emit(request);
            }
            catch (FormatException)
            {
                preferenceMessage.Foreground = Brushes.Firebrick;
                preferenceMessage.Text = "请填写有效数值：投递间隔至少 0.5 秒，每页停留 0.5–30 秒，字数为非负整数。时间最多三位小数。";
            }
        }

        internal void Receive(string line)
        {
            if (exiting || String.IsNullOrWhiteSpace(line)) return;
            string receivedCommandId = null;
            try
            {
                if (line.Length > Program.Json.MaxJsonLength) throw new InvalidReplyException("REPLY_TRANSPORT_LIMIT");
                Dictionary<string, object> message = Program.Json.Deserialize<Dictionary<string, object>>(line);
                if (message == null) throw new FormatException();
                // Extract correlation metadata without applying any display state.
                // The raw JSON check still rejects surrogates that the Framework
                // parser silently replaces, and can now identify the rejected command.
                object suppliedCommandId;
                if (message.TryGetValue("commandId", out suppliedCommandId))
                {
                    receivedCommandId = suppliedCommandId as string;
                    if (suppliedCommandId != null && receivedCommandId == null)
                        throw new InvalidReplyException("INVALID_COMMAND_ID");
                }
                ValidateJsonSurrogates(line);
                string type = ReadString(message, "type", true);
                if (type == "reply")
                {
                    // Finish all validation before replacing the active sequence.
                    string[] lines = ParseReply(message);
                    int holdMs = ParseHoldMs(message);
                    currentCommandId = receivedCommandId;
                    SetReplyLines(lines, holdMs);
                }
                else if (type == "status")
                {
                    status = ReadString(message, "text", true);
                    if (tray != null) tray.Text = TrimTrayText("LOL 陪玩桌宠 · " + status);
                }
                else if (type == "clear") { currentCommandId = receivedCommandId; ClearBubble(true); }
                else if (type == "show") ShowPet();
                else if (type == "hide") { currentCommandId = receivedCommandId; HidePet(); }
                else if (type == "preferences") ReceivePreferences(message);
                else if (type == "chat:result") ReceiveChatResult(message);
                else if (type == "chat:test" && Program.Smoke) ReceiveChatTest(message);
                else if (type == "shutdown") { ExitPet("shutdown"); return; }
                else throw new FormatException();
                if (Program.Smoke)
                    Program.Emit(new { type = "handled", messageType = type, replyLength = reply.Length, silent = reply.Length == 0, pendingLines = playback.Remaining, phase = playback.Phase, holdMs = playback.HoldMs, commandId = currentCommandId,
                        bindingId = preferenceBindingId, dataMode = conversationPreferences.DataMode, decisionIntervalMs = conversationPreferences.DecisionIntervalMs, pollIntervalMs = conversationPreferences.PollIntervalMs,
                        chatVisible = chatVisible, chatPending = chatRequestId != null, chatTextLength = chatText == null ? 0 : chatText.Text.Length });
            }
            catch (InvalidReplyException ex)
            {
                ReportInvalid(ex.Code, receivedCommandId);
                Program.Error("invalid-reply", ex);
            }
            catch (Exception ex)
            {
                ReportInvalid("INVALID_PROTOCOL", receivedCommandId);
                Program.Error("protocol", ex);
            }
        }

        private void ReportInvalid(string code, string receivedCommandId)
        {
            Program.Emit(new { type = "invalid", code = code, commandId = receivedCommandId, active = playback.Active, currentCommandId = currentCommandId });
        }

        private static void ValidateJsonSurrogates(string json)
        {
            // JavaScriptSerializer replaces a lone escaped surrogate with U+FFFD.
            // Reject it before parsing so malformed input is never rewritten.
            bool inString = false;
            for (int i = 0; i < json.Length; i++)
            {
                if (json[i] == '"') { inString = !inString; continue; }
                if (!inString || json[i] != '\\') continue;
                if (i + 5 < json.Length && json[i + 1] == 'u')
                {
                    int scalar;
                    if (!Int32.TryParse(json.Substring(i + 2, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out scalar)) continue;
                    if (scalar >= 0xD800 && scalar <= 0xDBFF)
                    {
                        int low;
                        if (i + 11 >= json.Length || json[i + 6] != '\\' || json[i + 7] != 'u' ||
                            !Int32.TryParse(json.Substring(i + 8, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out low) || low < 0xDC00 || low > 0xDFFF)
                            throw new InvalidReplyException("INVALID_UNICODE");
                        i += 11;
                    }
                    else
                    {
                        if (scalar >= 0xDC00 && scalar <= 0xDFFF) throw new InvalidReplyException("INVALID_UNICODE");
                        i += 5;
                    }
                }
                else i++; // An escaped backslash is literal text, not a Unicode escape.
            }
        }

        private static string[] ParseReply(Dictionary<string, object> message)
        {
            object supplied;
            bool hasLines = message.TryGetValue("lines", out supplied);
            if (hasLines && message.ContainsKey("text")) throw new InvalidReplyException("AMBIGUOUS_REPLY");
            if (hasLines)
            {
                IList list = supplied as IList;
                if (list == null || list.Count < 1) throw new InvalidReplyException("INVALID_LINE_COUNT");
                string[] result = new string[list.Count];
                for (int i = 0; i < list.Count; i++)
                {
                    string line = list[i] as string;
                    if (line == null) throw new InvalidReplyException("INVALID_LINE_TYPE");
                    if (line.Trim() == "<silent/>")
                    {
                        if (list.Count == 1) return new string[0];
                        throw new InvalidReplyException("MIXED_SILENT_REPLY");
                    }
                    ValidateSingleLine(line);
                    // Host Intl.Segmenter validates the 20-grapheme display limit.
                    // Framework StringInfo predates modern emoji/Indic clusters;
                    // do not reject a valid host-validated line with an older table.
                    if (line.Length > 4096) throw new InvalidReplyException("REPLY_TRANSPORT_LIMIT");
                    result[i] = line;
                }
                return result;
            }
            string text = ReadString(message, "text", true);
            if (String.IsNullOrWhiteSpace(text) || text.Trim() == "<silent/>") return new string[0];
            ValidateSingleLine(text);
            if (CountLegacyElements(text) > 20) throw new InvalidReplyException("REPLY_TOO_LONG");
            return new string[] { text };
        }

        private static int ParseHoldMs(Dictionary<string, object> message)
        {
            object value;
            if (!message.TryGetValue("holdMs", out value)) return BubblePlayback.DefaultHoldMs;
            if (!(value is int) && !(value is long) && !(value is decimal) && !(value is double))
                throw new InvalidReplyException("INVALID_HOLD_MS");
            double numeric = Convert.ToDouble(value, CultureInfo.InvariantCulture);
            if (!IsFinite(numeric) || numeric < 500 || numeric > 30000 || numeric != Math.Floor(numeric))
                throw new InvalidReplyException("INVALID_HOLD_MS");
            return (int)numeric;
        }

        private static void ValidateSingleLine(string text)
        {
            if (String.IsNullOrWhiteSpace(text)) throw new InvalidReplyException("EMPTY_REPLY_LINE");
            for (int i = 0; i < text.Length; i++)
            {
                if (Char.IsControl(text[i]) || text[i] == '\u2028' || text[i] == '\u2029') throw new InvalidReplyException("MULTILINE_REPLY");
                if (Char.IsHighSurrogate(text[i]))
                {
                    if (i + 1 >= text.Length || !Char.IsLowSurrogate(text[++i])) throw new InvalidReplyException("INVALID_UNICODE");
                }
                else if (Char.IsLowSurrogate(text[i])) throw new InvalidReplyException("INVALID_UNICODE");
            }
        }

        private static int CountLegacyElements(string value)
        {
            int count = 0, regional = 0;
            bool joined = false;
            TextElementEnumerator elements = StringInfo.GetTextElementEnumerator(value);
            while (elements.MoveNext())
            {
                string element = elements.GetTextElement();
                int scalar = Char.ConvertToUtf32(element, 0);
                if (scalar == 0x200D) { joined = true; continue; }
                if ((scalar >= 0x1F3FB && scalar <= 0x1F3FF) || (scalar >= 0xE0020 && scalar <= 0xE007F)) continue;
                if (scalar >= 0x1F1E6 && scalar <= 0x1F1FF)
                {
                    if (!joined && regional % 2 == 0) count++;
                    regional++; joined = false; continue;
                }
                regional = 0;
                if (joined) { joined = false; continue; }
                count++;
            }
            return count;
        }

        private void SetReplyLines(string[] lines, int holdMs = BubblePlayback.DefaultHoldMs)
        {
            if (lines.Length == 0 || userHidden || exiting) { ClearBubble(true); return; }
            // Manual replacement remains active throughout. A transient false
            // would let the host start another automatic turn between replies.
            playbackTimer.Stop();
            playback.Start(lines, holdMs);
            playbackClock.Restart();
            ApplyPlayback();
            ReportPlayback(true, true);
            playbackTimer.Start();
        }

        private void ReportPlayback(bool active, bool force)
        {
            if (!force && active == reportedPlaybackActive) return;
            reportedPlaybackActive = active;
            if (playbackObserver != null) playbackObserver(active);
            if (!Program.Rendering) Program.Emit(new { type = "playback", active = active, commandId = currentCommandId });
        }

        private void ApplyPlayback()
        {
            if (!playback.Active)
            {
                ClearBubble();
                return;
            }
            reply = playback.Text;
            if (bubbleText.Text != reply) { bubbleText.Text = reply; UpdateTextSize(); }
            bubbleLayer.Visibility = Visibility.Visible;
            bubbleLayer.Opacity = playback.Opacity;
            bubbleLayer.IsHitTestVisible = playback.Opacity > 0.01;
        }

        private void ClearBubble(bool notifyEvenIfIdle = false)
        {
            playbackTimer.Stop();
            playbackClock.Reset();
            playback.Clear();
            reply = "";
            bubbleText.Text = "";
            bubbleLayer.Opacity = 0;
            bubbleLayer.Visibility = Visibility.Collapsed;
            bubbleLayer.IsHitTestVisible = false;
            ReportPlayback(false, notifyEvenIfIdle);
        }

        private static string ReadString(Dictionary<string, object> message, string key, bool required)
        {
            object value;
            if (message.TryGetValue(key, out value) && value is string) return (string)value;
            if (required) throw new FormatException();
            return "";
        }

        private static string TrimTrayText(string text)
        {
            text = text.Replace('\r', ' ').Replace('\n', ' ');
            if (text.Length <= 60) return text;
            int end = Char.IsHighSurrogate(text[59]) ? 59 : 60;
            return text.Substring(0, end);
        }

        internal void ExitPet(string reason)
        {
            if (exiting) return;
            exiting = true;
            DismissChatInput(true);
            if (chatWindow != null) chatWindow.Close();
            ClearBubble(true);
            SaveSettings();
            DisposeResources();
            Program.Emit(new { type = "exit", reason = reason });
            Application.Current.Shutdown();
        }

        internal void DisposeResources()
        {
            if (resourcesDisposed) return;
            resourcesDisposed = true;
            ClearBubble();
            if (tray != null) { tray.Visible = false; tray.Dispose(); }
        }

        internal void RenderTest(string outputPath)
        {
            Width = Height = 480;
            settings.FontSize = 16;
            UpdateTextSize();
            SetReplyLines(new string[] { "这波击杀，我看见啦。", "下一波也陪着你。" });
            playback.Advance(250);
            ApplyPlayback();
            SavePreview(outputPath);
            bool bubbleAcceptsClicks = IsInteractiveAt(new Point(164, Canvas.GetTop(bubbleLayer) + 40));
            bool characterRemains = characterImage.Opacity == 1 && characterImage.Visibility == Visibility.Visible;
            playback.Advance(5375);
            ApplyPlayback();
            bool fadesOnlyBubble = Math.Abs(bubbleLayer.Opacity - 0.5) < 0.001 && characterImage.Opacity == 1;
            playback.Advance(5750);
            ApplyPlayback();
            bool secondInOrder = reply == "下一波也陪着你。" && playback.Remaining == 0;
            playback.Advance(11000);
            ApplyPlayback();
            bool completes = !playback.Active && !playbackTimer.IsEnabled && bubbleLayer.Visibility == Visibility.Collapsed;
            bool vanishedBubblePassesClicks = !IsInteractiveAt(new Point(164, 300));
            string idlePath = Path.Combine(Path.GetDirectoryName(outputPath), Path.GetFileNameWithoutExtension(outputPath) + "-idle.png");
            SavePreview(idlePath);

            SetReplyLines(new string[] { "第一句", "第二句" });
            ClearBubble();
            bool clearCancels = !playback.Active && playback.Remaining == 0 && !playbackTimer.IsEnabled;
            SetReplyLines(new string[] { "第一句", "第二句" });
            HidePet();
            SetReplyLines(new string[] { "隐藏时不重播" });
            bool hideCancels = !playback.Active && playback.Remaining == 0 && !playbackTimer.IsEnabled;
            ShowPet();
            bool showDoesNotReplay = !playback.Active;
            SetReplyLines(ParseReply(new Dictionary<string, object> { { "text", "  <silent/>\r\n" } }));
            bool silentClears = bubbleText.Text.Length == 0 && !playbackTimer.IsEnabled;
            bool longRejected = false;
            try { ParseReply(new Dictionary<string, object> { { "text", new string('字', 21) } }); }
            catch (InvalidReplyException) { longRejected = true; }
            string emoji = "\U0001F469\u200D\U0001F4BB";
            bool emojiCompatible = CountLegacyElements(emoji) == 1 && ParseReply(new Dictionary<string, object> { { "lines", new string[] { emoji } } })[0] == emoji;

            Width = Height = 240;
            settings.FontSize = 28;
            SetReplyLines(new string[] { new string('字', 20) });
            playback.Advance(250); ApplyPlayback();
            SavePreview(Path.Combine(Path.GetDirectoryName(outputPath), Path.GetFileNameWithoutExtension(outputPath) + "-compact.png"));
            bool fitsCompact = bubbleText.Text.Length == 20 && bubbleText.DesiredSize.Height <= 250 && Canvas.GetTop(bubbleLayer) >= 0;
            string twentyEmoji = String.Concat(new string[] { emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji, emoji });
            SetReplyLines(new string[] { twentyEmoji });
            playback.Advance(250); ApplyPlayback();
            SavePreview(Path.Combine(Path.GetDirectoryName(outputPath), Path.GetFileNameWithoutExtension(outputPath) + "-emoji.png"));
            bool fitsEmoji = bubbleText.Text == twentyEmoji && Canvas.GetTop(bubbleLayer) >= 0 && bubbleText.DesiredSize.Height <= 250;
            ClearBubble();
            bool alphaPresent = alphaPixels[3] == 0;
            bool removedFixedBubble = alphaPixels[200 * alphaStride + 400 * 4 + 3] == 0 && alphaPixels[560 * alphaStride + 350 * 4 + 3] == 0 && alphaPixels[650 * alphaStride + 440 * 4 + 3] == 0;
            BubblePlayback timeline = new BubblePlayback();
            timeline.Start(new string[] { "a", "b" }); timeline.Advance(125);
            bool timing = timeline.Phase == "fade-in" && Math.Abs(timeline.Opacity - 0.5) < 0.001;
            timeline.Advance(250); timing &= timeline.Phase == "hold" && timeline.Opacity == 1;
            timeline.Advance(5249); timing &= timeline.Phase == "hold";
            timeline.Advance(5250); timing &= timeline.Phase == "fade-out";
            timeline.Advance(5500); timing &= timeline.Text == "b" && timeline.Phase == "fade-in";
            timeline.Advance(11000); timing &= !timeline.Active;

            // More than two pages retain their full order, including the last
            // page. Test every page without waiting minutes on wall-clock time.
            string[] manyPages = new string[257];
            for (int i = 0; i < manyPages.Length; i++) manyPages[i] = "page " + i;
            List<bool> notifications = new List<bool>();
            playbackObserver = delegate(bool active) { notifications.Add(active); };
            SetReplyLines(ParseReply(new Dictionary<string, object> { { "lines", manyPages } }), 500);
            bool allPagesInOrder = playback.Remaining == 256 && playback.HoldMs == 500;
            for (int i = 0; i < manyPages.Length; i++)
            {
                playback.Advance(i * 1000 + 250);
                ApplyPlayback();
                allPagesInOrder &= reply == manyPages[i] && playback.Phase == "hold" && playback.Remaining == manyPages.Length - i - 1;
            }
            playback.Advance(manyPages.Length * 1000);
            ApplyPlayback();
            bool naturalEndNotifies = !playback.Active && notifications.Count == 2 && notifications[0] && !notifications[1];
            notifications.Clear();
            SetReplyLines(new string[] { "manual one" }, 30000);
            SetReplyLines(new string[] { "manual replacement" }, 1000);
            bool replacementStaysActive = notifications.Count == 2 && notifications[0] && notifications[1] && reply == "manual replacement";
            ClearBubble(true);
            bool clearNotifies = notifications.Count == 3 && !notifications[2];
            SetReplyLines(new string[] { "hide test" });
            HidePet();
            SetReplyLines(new string[] { "discarded hidden reply" });
            bool hideNotifies = notifications.Count == 6 && notifications[3] && !notifications[4] && !notifications[5] && !playback.Active;
            ShowPet();
            SetReplyLines(new string[] { "silent test" });
            SetReplyLines(new string[0]);
            bool silentNotifies = notifications.Count == 8 && notifications[6] && !notifications[7];
            playbackObserver = null;

            timeline.Start(new string[] { "a", "b", "c" }, 500);
            timeline.Advance(749);
            bool configurableTiming = timeline.Phase == "hold" && timeline.HoldMs == 500;
            timeline.Advance(750); configurableTiming &= timeline.Phase == "fade-out";
            timeline.Advance(875); configurableTiming &= Math.Abs(timeline.Opacity - 0.5) < 0.001;
            timeline.Advance(1000); configurableTiming &= timeline.Text == "b" && timeline.Phase == "fade-in";
            timeline.Advance(3000); configurableTiming &= !timeline.Active;
            timeline.Start(new string[] { "slow" }, 30000);
            timeline.Advance(30249); configurableTiming &= timeline.Phase == "hold";
            timeline.Advance(30250); configurableTiming &= timeline.Phase == "fade-out";
            timeline.Advance(30500); configurableTiming &= !timeline.Active;
            bool preferencesVerified = TestConversationSettings(outputPath);
            bool chatVerified = TestChatInput(outputPath);
            int scrollViewers = CountScrollViewers(artwork);
            bool ok = characterRemains && fadesOnlyBubble && secondInOrder && completes && clearCancels && hideCancels && showDoesNotReplay && silentClears && longRejected && emojiCompatible && fitsCompact && fitsEmoji && alphaPresent && removedFixedBubble && timing && bubbleAcceptsClicks && vanishedBubblePassesClicks && allPagesInOrder && naturalEndNotifies && replacementStaysActive && clearNotifies && hideNotifies && silentNotifies && configurableTiming && preferencesVerified && chatVerified && scrollViewers == 0;
            Program.Emit(new
            {
                type = "render-test",
                ok = ok,
                path = outputPath,
                idlePath = idlePath,
                imageWidth = picture.PixelWidth,
                imageHeight = picture.PixelHeight,
                transparentBackground = alphaPresent,
                originalBubbleRemoved = removedFixedBubble,
                characterRemains = characterRemains,
                fadesOnlyBubble = fadesOnlyBubble,
                secondInOrder = secondInOrder,
                completedAfterTwoLines = completes,
                clearCancels = clearCancels,
                hideCancels = hideCancels,
                showDoesNotReplay = showDoesNotReplay,
                longRejected = longRejected,
                emojiCompatible = emojiCompatible,
                fitsCompact = fitsCompact,
                twentyJoinedEmojiFit = fitsEmoji,
                timing250_5000_250 = timing,
                bubbleAcceptsClicks = bubbleAcceptsClicks,
                vanishedBubblePassesClicks = vanishedBubblePassesClicks,
                silentClears = silentClears,
                all257PagesInOrder = allPagesInOrder,
                naturalEndNotifies = naturalEndNotifies,
                replacementStaysActive = replacementStaysActive,
                clearNotifies = clearNotifies,
                hideNotifies = hideNotifies,
                silentNotifies = silentNotifies,
                configurableTiming500_30000 = configurableTiming,
                conversationSettings = preferencesVerified,
                chatInput = chatVerified,
                chatInputChecks = chatTestResults,
                scrollViewerCount = scrollViewers
            });
            if (!ok) throw new InvalidOperationException("Render checks failed.");
        }

        private static Dictionary<string, object> PreferenceSnapshot(string bindingId, int interval, int poll, int hold, long length, string mode, string requestId = null, string error = null)
        {
            Dictionary<string, object> result = new Dictionary<string, object>
            {
                { "bindingId", bindingId },
                { "values", new Dictionary<string, object> { { "decisionIntervalMs", interval }, { "pollIntervalMs", poll },
                    { "bubbleHoldMs", hold }, { "replyLength", length }, { "dataMode", mode } } }
            };
            if (requestId != null) result["requestId"] = requestId;
            if (error != null) result["error"] = error;
            return result;
        }

        private bool TestConversationSettings(string outputPath)
        {
            StackPanel section = BuildConversationSettings();
            List<Dictionary<string, object>> requests = new List<Dictionary<string, object>>();
            preferenceRequestObserver = delegate(object request) { requests.Add(Program.Json.Deserialize<Dictionary<string, object>>(Program.Json.Serialize(request))); };
            bool unboundDisabled = !preferenceControls.IsEnabled && conversationPreferences.DataMode == "basic";
            ApplyConversationPreferences();
            unboundDisabled &= requests.Count == 0;
            currentCommandId = "settings-playback";
            SetReplyLines(new string[] { "设置不会打断播放" });
            ReceivePreferences(PreferenceSnapshot("binding-A", 15000, 1000, 5000, 20, "detailed"));
            bool hydrated = preferenceControls.IsEnabled && preferenceInterval.Text == Seconds(15000) && preferenceLength.Text == "20" &&
                conversationPreferences.DataMode == "detailed" && (string)((ComboBoxItem)preferenceDataMode.SelectedItem).Tag == "detailed";

            // Render the actual editable conversation controls in the same
            // scroll container used by the settings window, without showing UI.
            ScrollViewer scroll = new ScrollViewer
            {
                Content = section, Background = Brushes.White, Padding = new Thickness(22),
                FontFamily = new FontFamily("Microsoft YaHei UI"), FontSize = 14,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
            };
            SaveElementPreview(scroll, 400, 640, Path.Combine(Path.GetDirectoryName(outputPath), Path.GetFileNameWithoutExtension(outputPath) + "-settings.png"));

            preferenceInterval.Text = "0.5";
            preferenceDataMode.SelectedIndex = 0;
            preferenceHold.Text = "1";
            preferenceLength.Text = "0";
            ApplyConversationPreferences();
            Dictionary<string, object> first = requests.Count == 1 ? requests[0] : new Dictionary<string, object>();
            Dictionary<string, object> changes = first.ContainsKey("changes") ? first["changes"] as Dictionary<string, object> : null;
            bool requestOnly = changes != null && changes.Count == 4 && Convert.ToInt32(changes["decisionIntervalMs"]) == 500 &&
                Convert.ToInt32(changes["bubbleHoldMs"]) == 1000 && Convert.ToInt64(changes["replyLength"]) == 0 &&
                (string)changes["dataMode"] == "basic" && (string)first["bindingId"] == "binding-A" &&
                !preferenceControls.IsEnabled && conversationPreferences.DecisionIntervalMs == 15000;
            string firstId = preferencePendingRequestId;
            ReceivePreferences(PreferenceSnapshot("binding-A", 500, 500, 1000, 0, "basic", firstId));
            bool confirmed = preferenceControls.IsEnabled && preferencePendingRequestId == null && conversationPreferences.PollIntervalMs == 500 &&
                preferenceSampling.Text.Contains(Seconds(500)) && preferenceInterval.Text == Seconds(500);

            preferenceInterval.Text = "0.25";
            ApplyConversationPreferences();
            bool invalidLocal = requests.Count == 1 && preferencePendingRequestId == null && preferenceControls.IsEnabled;
            preferenceInterval.Text = "0.75";
            ApplyConversationPreferences();
            string secondId = preferencePendingRequestId;
            bool customInterval = requests.Count == 2 && Convert.ToInt32(((Dictionary<string, object>)requests[1]["changes"])["decisionIntervalMs"]) == 750;
            ReceivePreferences(PreferenceSnapshot("binding-A", 500, 500, 1000, 0, "basic", secondId, "保存失败"));
            bool rejectedRestores = preferenceControls.IsEnabled && preferencePendingRequestId == null && preferenceInterval.Text == Seconds(500) &&
                preferenceMessage.Text.Contains("保存失败");
            preferenceInterval.Text = "10";
            ReceivePreferences(PreferenceSnapshot("binding-B", 2000, 1000, 5000, 40, "detailed"));
            bool switchDropsDraft = preferenceInterval.Text == Seconds(2000) && preferenceLength.Text == "40" && preferenceBindingId == "binding-B";
            ReceivePreferences(PreferenceSnapshot("binding-A", 750, 500, 1000, 0, "basic", secondId));
            bool staleIgnored = preferenceBindingId == "binding-B" && preferenceInterval.Text == Seconds(2000);
            preferenceInterval.Text = "3.25";
            ApplyConversationPreferences();
            string thirdId = preferencePendingRequestId;
            ReceivePreferences(PreferenceSnapshot("binding-C", 15000, 1000, 5000, 20, "detailed"));
            bool switchDropsPending = preferencePendingRequestId == null && preferenceControls.IsEnabled;
            ReceivePreferences(PreferenceSnapshot("binding-B", 3250, 1000, 5000, 40, "detailed", thirdId));
            staleIgnored &= preferenceBindingId == "binding-C" && conversationPreferences.DecisionIntervalMs == 15000;
            bool malformedRejected = false;
            try { ReceivePreferences(PreferenceSnapshot("binding-C", 499, 1000, 5000, 20, "detailed")); }
            catch (InvalidReplyException) { malformedRejected = conversationPreferences.DecisionIntervalMs == 15000; }
            ReceivePreferences(new Dictionary<string, object> { { "bindingId", null } });
            unboundDisabled &= !preferenceControls.IsEnabled && preferenceBindingId == null && conversationPreferences.DataMode == "basic";
            bool displayUnaffected = playback.Active && playback.Text == "设置不会打断播放" && currentCommandId == "settings-playback";
            ClearBubble();
            preferenceRequestObserver = null;
            preferenceControls = null;
            return unboundDisabled && hydrated && requestOnly && confirmed && invalidLocal && customInterval && rejectedRestores &&
                switchDropsDraft && staleIgnored && switchDropsPending && malformedRejected && displayUnaffected;
        }

        private static void SaveElementPreview(FrameworkElement element, double width, double height, string outputPath)
        {
            element.Measure(new Size(width, height));
            element.Arrange(new Rect(0, 0, width, height));
            element.UpdateLayout();
            RenderTargetBitmap target = new RenderTargetBitmap((int)(width * 2), (int)(height * 2), 192, 192, PixelFormats.Pbgra32);
            target.Render(element);
            PngBitmapEncoder encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(target));
            Directory.CreateDirectory(Path.GetDirectoryName(outputPath));
            using (FileStream stream = File.Create(outputPath)) encoder.Save(stream);
        }

        private void SavePreview(string outputPath)
        {
            artwork.Measure(new Size(Width, Height));
            artwork.Arrange(new Rect(0, 0, Width, Height));
            artwork.UpdateLayout();
            RenderTargetBitmap target = new RenderTargetBitmap((int)(Width * 2), (int)(Height * 2), 192, 192, PixelFormats.Pbgra32);
            target.Render(artwork);
            PngBitmapEncoder encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(target));
            Directory.CreateDirectory(Path.GetDirectoryName(outputPath));
            using (FileStream stream = File.Create(outputPath)) encoder.Save(stream);
        }

        private static int CountScrollViewers(DependencyObject value)
        {
            int result = value is ScrollViewer ? 1 : 0;
            int children = VisualTreeHelper.GetChildrenCount(value);
            for (int i = 0; i < children; i++) result += CountScrollViewers(VisualTreeHelper.GetChild(value, i));
            return result;
        }

        private static bool IsFinite(double value) { return !Double.IsNaN(value) && !Double.IsInfinity(value); }
        private static double Clamp(double value, double min, double max, double fallback) { return IsFinite(value) ? Math.Max(min, Math.Min(max, value)) : fallback; }
        private static DependencyObject ParentOf(DependencyObject value)
        {
            if (value is Visual || value is System.Windows.Media.Media3D.Visual3D) return VisualTreeHelper.GetParent(value);
            return LogicalTreeHelper.GetParent(value);
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct NativePoint { public int X; public int Y; }
        [DllImport("user32.dll")] private static extern bool GetCursorPos(out NativePoint point);
        [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] private static extern IntPtr GetWindowLongPtr64(IntPtr hwnd, int index);
        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")] private static extern IntPtr SetWindowLongPtr64(IntPtr hwnd, int index, IntPtr value);
        [DllImport("user32.dll", EntryPoint = "GetWindowLongW")] private static extern int GetWindowLong32(IntPtr hwnd, int index);
        [DllImport("user32.dll", EntryPoint = "SetWindowLongW")] private static extern int SetWindowLong32(IntPtr hwnd, int index, int value);
        [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
        private static IntPtr GetWindowLongPtr(IntPtr hwnd, int index) { return IntPtr.Size == 8 ? GetWindowLongPtr64(hwnd, index) : new IntPtr(GetWindowLong32(hwnd, index)); }
        private static IntPtr SetWindowLongPtr(IntPtr hwnd, int index, IntPtr value) { return IntPtr.Size == 8 ? SetWindowLongPtr64(hwnd, index, value) : new IntPtr(SetWindowLong32(hwnd, index, value.ToInt32())); }
    }
}
