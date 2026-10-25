using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;

namespace LolCompanionDesktop
{
    // A presentation source for routed-event verification without an HWND.
    internal sealed class ChatTestSource : PresentationSource
    {
        public override Visual RootVisual { get; set; }
        public override bool IsDisposed { get { return false; } }
        protected override CompositionTarget GetCompositionTargetCore() { return null; }
    }
    // Counts completed clicks only. Crossing the system drag threshold cancels
    // the whole sequence, even if the pointer comes back before mouse-up.
    internal sealed class CharacterClickGesture
    {
        private Point down, previous;
        private bool eligible, moved;
        private int count, previousTime;
        internal void Down(Point point, bool character) { down = point; eligible = character; moved = false; if (!character) count = 0; }
        internal bool Move(Point point)
        {
            if (Math.Abs(point.X - down.X) >= SystemParameters.MinimumHorizontalDragDistance ||
                Math.Abs(point.Y - down.Y) >= SystemParameters.MinimumVerticalDragDistance) { moved = true; count = 0; }
            return moved;
        }
        internal bool Up(Point point, int time)
        {
            Move(point);
            if (!eligible || moved) { Cancel(); return false; }
            uint elapsed = unchecked((uint)(time - previousTime));
            bool close = Math.Abs(point.X - previous.X) <= System.Windows.Forms.SystemInformation.DoubleClickSize.Width &&
                Math.Abs(point.Y - previous.Y) <= System.Windows.Forms.SystemInformation.DoubleClickSize.Height;
            count = count > 0 && elapsed <= System.Windows.Forms.SystemInformation.DoubleClickTime && close ? count + 1 : 1;
            previous = point; previousTime = time; eligible = false;
            if (count < 3) return false;
            count = 0; return true;
        }
        internal void Cancel() { count = 0; eligible = false; moved = false; }
    }

    internal sealed partial class CompanionWindow
    {
        private const int ChatTextLimit = 65536;
        private readonly CharacterClickGesture clickGesture = new CharacterClickGesture();
        private Window chatWindow;
        private Border chatPanel;
        private TextBox chatText;
        private TextBlock chatMessage;
        private DispatcherTimer chatTimeout;
        private string chatRequestId, chatRequestText, chatRequestBinding;
        private bool chatVisible, chatCanRetry, positioningChat, imeComposing, imeEnterGuard;
        private Action<object> chatRequestObserver;
        private Dictionary<string, bool> chatTestResults;
        private IntPtr chatPreviousForeground;

        private void EnsureChatInput()
        {
            if (chatPanel != null) return;
            chatPanel = new Border
            {
                Background = new SolidColorBrush(Color.FromArgb(64, 15, 23, 42)),
                BorderBrush = new SolidColorBrush(Color.FromArgb(90, 220, 230, 255)),
                BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8), Padding = new Thickness(8)
            };
            Grid grid = new Grid();
            grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            chatText = new TextBox
            {
                AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, Padding = new Thickness(2), MinHeight = 30,
                MaxLength = ChatTextLimit, FontSize = 14, Background = Brushes.Transparent,
                BorderThickness = new Thickness(0), Foreground = Brushes.White, CaretBrush = Brushes.White
            };
            Grid.SetRow(chatText, 0); grid.Children.Add(chatText);
            TextCompositionManager.AddPreviewTextInputStartHandler(chatText, delegate { BeginImeComposition(); });
            TextCompositionManager.AddPreviewTextInputUpdateHandler(chatText, delegate { BeginImeComposition(); });
            TextCompositionManager.AddPreviewTextInputHandler(chatText, delegate
            {
                if (!imeComposing) return;
                CompleteImeComposition();
                // Mouse-selected candidates have no pending Enter key-up. Clear
                // only after input processing and only when Enter is not held.
                Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(delegate
                {
                    if (!imeComposing && !Keyboard.IsKeyDown(Key.Enter)) imeEnterGuard = false;
                }));
            });
            chatText.PreviewKeyDown += ChatKeyDown;
            chatText.PreviewKeyUp += delegate(object sender, KeyEventArgs e)
            {
                if ((e.Key == Key.Enter || e.Key == Key.ImeProcessed) && !imeComposing) imeEnterGuard = false;
            };
            chatMessage = new TextBlock { FontSize = 11, Foreground = Brushes.White, TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(2, 4, 0, 0), Visibility = Visibility.Collapsed };
            Grid.SetRow(chatMessage, 1); grid.Children.Add(chatMessage);
            chatPanel.Child = grid;
            chatPanel.SetValue(TextElement.FontFamilyProperty, new FontFamily("Microsoft YaHei UI"));
            if (!headless)
            {
                chatWindow = new Window
                {
                    Title = "和桌宠聊天", WindowStyle = WindowStyle.None, ResizeMode = ResizeMode.NoResize,
                    AllowsTransparency = true, Background = Brushes.Transparent, Content = chatPanel,
                    Width = 360, Height = 64, Topmost = true, ShowInTaskbar = false, ShowActivated = true,
                    WindowStartupLocation = WindowStartupLocation.Manual
                };
                chatWindow.PreviewKeyDown += delegate(object sender, KeyEventArgs e)
                {
                    if (e.Key == Key.Escape && !imeComposing && !imeEnterGuard) { DismissChatInput(false); e.Handled = true; }
                };
                chatWindow.Closing += delegate(object sender, System.ComponentModel.CancelEventArgs e)
                {
                    if (!exiting) { e.Cancel = true; DismissChatInput(false); }
                };
            }
            chatTimeout = new DispatcherTimer(DispatcherPriority.Background, Dispatcher) { Interval = TimeSpan.FromSeconds(15) };
            chatTimeout.Tick += delegate { ChatConfirmationTimedOut(); };
        }

        private void BeginImeComposition() { imeComposing = true; imeEnterGuard = true; }
        private void CompleteImeComposition() { imeComposing = false; imeEnterGuard = true; }
        private bool CanSubmitKey(Key key, bool shift) { return key == Key.Enter && !shift && !imeComposing && !imeEnterGuard; }
        private void ChatKeyDown(object sender, KeyEventArgs e)
        {
            // ImeProcessed and composition-confirming Enter belong to the IME.
            // Never mark them handled, or WPF may lose the selected candidate.
            if (e.Key == Key.ImeProcessed || imeComposing || imeEnterGuard) return;
            if (e.Key == Key.Escape) { DismissChatInput(false); e.Handled = true; return; }
            if (CanSubmitKey(e.Key, (Keyboard.Modifiers & ModifierKeys.Shift) != 0))
            {
                SubmitChatInput(); e.Handled = true;
            }
        }

        private void OpenChatInput()
        {
            if (exiting) return;
            EnsureChatInput();
            ShowPet();
            chatVisible = true;
            RefreshChatInput(null);
            PositionChatInput();
            if (!headless)
            {
                // Called only from an explicit click/menu action. Incoming
                // preferences/results never open or activate this window.
                IntPtr foreground = GetForegroundWindow();
                if (foreground != new WindowInteropHelper(chatWindow).Handle) chatPreviousForeground = foreground;
                chatWindow.Show(); chatWindow.Activate(); chatText.Focus();
            }
        }

        private void RefreshChatInput(string message)
        {
            if (chatText == null) return;
            bool bound = preferenceBindingId != null;
            chatText.IsEnabled = bound;
            chatText.IsReadOnly = chatRequestId != null;
            chatMessage.Text = !bound ? "请先开启陪玩" : message ?? "";
            chatMessage.Visibility = chatMessage.Text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
            PositionChatInput();
        }

        private void DismissChatInput(bool discard)
        {
            IntPtr handle = chatWindow == null ? IntPtr.Zero : new WindowInteropHelper(chatWindow).Handle;
            bool restoreFocus = !headless && ShouldRestoreChatFocus(chatVisible, GetForegroundWindow(), handle, chatPreviousForeground);
            chatVisible = false;
            if (chatWindow != null) chatWindow.Hide();
            // A late acknowledgement must not steal focus after the user has
            // switched to another app. Native focus restoration is best effort.
            if (restoreFocus && IsWindow(chatPreviousForeground)) SetForegroundWindow(chatPreviousForeground);
            imeComposing = false; imeEnterGuard = false;
            if (!discard) return;
            if (chatTimeout != null) chatTimeout.Stop();
            chatRequestId = chatRequestText = chatRequestBinding = null; chatCanRetry = false;
            if (chatText != null) chatText.Clear();
            RefreshChatInput(null);
        }

        private void SubmitChatInput()
        {
            if (!chatVisible || preferenceBindingId == null || chatText == null || imeComposing) return;
            if (chatRequestId != null && !chatCanRetry) return;
            if (chatRequestId == null)
            {
                string text = chatText.Text;
                if (String.IsNullOrWhiteSpace(text)) return;
                if (text.Length > ChatTextLimit || !WellFormedChat(text)) { RefreshChatInput("内容过长或无效"); return; }
                chatRequestId = Guid.NewGuid().ToString("N");
                chatRequestText = text; chatRequestBinding = preferenceBindingId;
            }
            if (chatRequestBinding != preferenceBindingId) { DismissChatInput(true); return; }
            chatCanRetry = false;
            RefreshChatInput(null);
            chatTimeout.Stop(); chatTimeout.Start();
            object request = new { type = "chat:submit", bindingId = chatRequestBinding, requestId = chatRequestId, text = chatRequestText };
            if (chatRequestObserver != null) chatRequestObserver(request);
            if (!Program.Rendering) Program.Emit(request);
        }

        private static bool WellFormedChat(string value)
        {
            for (int i = 0; i < value.Length; i++)
            {
                if (Char.IsHighSurrogate(value[i])) { if (++i >= value.Length || !Char.IsLowSurrogate(value[i])) return false; }
                else if (Char.IsLowSurrogate(value[i])) return false;
                else if (Char.IsControl(value[i]) && value[i] != '\r' && value[i] != '\n' && value[i] != '\t') return false;
            }
            return true;
        }

        private void ChatConfirmationTimedOut()
        {
            if (chatTimeout != null) chatTimeout.Stop();
            if (chatRequestId == null) return;
            chatCanRetry = true;
            RefreshChatInput("未确认，按回车重试");
        }

        private void ReceiveChatResult(Dictionary<string, object> message)
        {
            string bindingId = ReadString(message, "bindingId", true), requestId = ReadString(message, "requestId", true);
            object accepted;
            if (!message.TryGetValue("accepted", out accepted) || !(accepted is bool)) throw new InvalidReplyException("INVALID_CHAT_RESULT");
            if (bindingId != preferenceBindingId || bindingId != chatRequestBinding || requestId != chatRequestId) return;
            chatTimeout.Stop();
            chatRequestId = chatRequestText = chatRequestBinding = null; chatCanRetry = false;
            if ((bool)accepted) { chatText.Clear(); DismissChatInput(false); RefreshChatInput(null); }
            // Never display raw host exceptions/paths. The user can retry without losing the draft.
            else RefreshChatInput("发送失败，按回车重试");
        }

        internal static Rect ChatPlacement(Rect pet, Rect area, double width, double height)
        {
            width = Math.Min(width, area.Width); height = Math.Min(height, area.Height);
            return new Rect(Math.Max(area.Left, Math.Min(pet.Left + (pet.Width - width) / 2, area.Right - width)),
                Math.Max(area.Top, Math.Min(pet.Bottom + 6, area.Bottom - height)), width, height);
        }

        private static bool ShouldRestoreChatFocus(bool visible, IntPtr foreground, IntPtr composer, IntPtr previous)
        {
            return visible && composer != IntPtr.Zero && foreground == composer && previous != IntPtr.Zero && previous != composer;
        }

        [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr window);
        [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr window);

        private void PositionChatInput()
        {
            if (!chatVisible || chatWindow == null || positioningChat || exiting) return;
            positioningChat = true;
            try
            {
                Rect area = WorkAreaForWindow();
                double width = Math.Min(Math.Max(280, Width), Math.Min(380, area.Width));
                double height = chatMessage.Visibility == Visibility.Visible ? 86 : 64;
                // Keep the requested size; move the pair up before considering
                // overlap on an unusually short work area.
                double top = Math.Max(area.Top, Math.Min(Top, area.Bottom - Height - height - 6));
                if (top != Top) Top = top;
                Rect placement = ChatPlacement(new Rect(Left, Top, Width, Height), area, width, height);
                chatWindow.Width = placement.Width; chatWindow.Height = placement.Height;
                chatWindow.Left = placement.Left; chatWindow.Top = placement.Top;
            }
            finally { positioningChat = false; }
        }

        // Headless integration commands exist only in --smoke-test; the normal
        // protocol cannot open a composer or manufacture a user submission.
        private void ReceiveChatTest(Dictionary<string, object> message)
        {
            string action = ReadString(message, "action", true);
            if (action == "open") OpenChatInput();
            else if (action == "text") { EnsureChatInput(); if (!chatText.IsReadOnly) chatText.Text = ReadString(message, "text", true); }
            else if (action == "submit") SubmitChatInput();
            else if (action == "timeout") ChatConfirmationTimedOut();
            else if (action == "dismiss") DismissChatInput(false);
            else throw new InvalidReplyException("INVALID_CHAT_TEST");
        }

        private bool TestChatInput(string outputPath)
        {
            List<Dictionary<string, object>> requests = new List<Dictionary<string, object>>();
            chatRequestObserver = delegate(object value) { requests.Add(Program.Json.Deserialize<Dictionary<string, object>>(Program.Json.Serialize(value))); };
            ReceivePreferences(new Dictionary<string, object> { { "bindingId", null } });
            OpenChatInput();
            chatText.Text = "尚未绑定"; SubmitChatInput();
            bool unbound = chatVisible && !chatText.IsEnabled && requests.Count == 0 && chatMessage.Text.Contains("开启陪玩");
            ReceivePreferences(PreferenceSnapshot("chat-A", 15000, 1000, 5000, 20, "basic"));
            bool bindingCloses = !chatVisible && chatText.Text == "";
            OpenChatInput();
            currentCommandId = "chat-does-not-change-playback";
            SetReplyLines(new string[] { "聊天也不打断气泡" }, 30000);
            chatText.Text = "刚才那波你看见了吗？\n下一波陪我一起看，记得给我加油。";
            SaveElementPreview(chatPanel, 360, 64, Path.Combine(Path.GetDirectoryName(outputPath), "preview-chat.png"));
            bool multiline = chatText.AcceptsReturn && !CanSubmitKey(Key.Enter, true) && !CanSubmitKey(Key.ImeProcessed, false);

            // Exercise the actual routed composition and key handlers. This is
            // a WPF event test; an installed IME still needs a normal UI check.
            TextComposition composition = new TextComposition(InputManager.Current, chatText, "中");
            ChatTestSource keySource = new ChatTestSource();
            chatText.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, composition) { RoutedEvent = TextCompositionManager.PreviewTextInputStartEvent });
            KeyEventArgs enter = new KeyEventArgs(Keyboard.PrimaryDevice, keySource, 1, Key.Enter) { RoutedEvent = Keyboard.PreviewKeyDownEvent };
            chatText.RaiseEvent(enter);
            bool imeSafe = imeComposing && requests.Count == 0 && !enter.Handled;
            chatText.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, composition) { RoutedEvent = TextCompositionManager.PreviewTextInputEvent });
            chatText.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice, keySource, 2, Key.Enter) { RoutedEvent = Keyboard.PreviewKeyDownEvent });
            imeSafe &= !imeComposing && imeEnterGuard && requests.Count == 0;
            chatText.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice, keySource, 3, Key.Enter) { RoutedEvent = Keyboard.PreviewKeyUpEvent });
            chatText.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice, keySource, 4, Key.Enter) { RoutedEvent = Keyboard.PreviewKeyDownEvent });
            string firstId = chatRequestId;
            bool sent = requests.Count == 1 && firstId != null && (string)requests[0]["text"] == chatText.Text &&
                (string)requests[0]["bindingId"] == "chat-A" && chatText.IsReadOnly && chatVisible;
            SubmitChatInput();
            bool noDuplicate = requests.Count == 1;
            ChatConfirmationTimedOut(); SubmitChatInput();
            bool sameIdRetry = requests.Count == 2 && (string)requests[1]["requestId"] == firstId && (string)requests[1]["text"] == (string)requests[0]["text"];
            ReceiveChatResult(new Dictionary<string, object> { { "bindingId", "chat-A" }, { "requestId", "stale" }, { "accepted", true } });
            bool staleIgnored = chatRequestId == firstId && chatVisible && chatText.Text.Length > 20;
            ReceiveChatResult(new Dictionary<string, object> { { "bindingId", "chat-A" }, { "requestId", firstId }, { "accepted", false }, { "error", "private-path-should-not-show" } });
            bool failureDraft = chatRequestId == null && !chatText.IsReadOnly && chatText.Text.Length > 20 && !chatMessage.Text.Contains("private-path");
            SubmitChatInput(); string secondId = chatRequestId;
            bool freshId = requests.Count == 3 && secondId != firstId;
            DismissChatInput(false);
            ReceiveChatResult(new Dictionary<string, object> { { "bindingId", "chat-A" }, { "requestId", secondId }, { "accepted", true } });
            bool successCloses = !chatVisible && chatRequestId == null && chatText.Text == "";
            OpenChatInput(); chatText.Text = "另一条草稿"; SubmitChatInput(); string oldId = chatRequestId;
            ReceivePreferences(PreferenceSnapshot("chat-B", 15000, 1000, 5000, 20, "basic"));
            bool switchClears = !chatVisible && chatText.Text == "" && chatRequestId == null;
            OpenChatInput(); chatText.Text = "新会话草稿";
            ReceiveChatResult(new Dictionary<string, object> { { "bindingId", "chat-A" }, { "requestId", oldId }, { "accepted", true } });
            staleIgnored &= chatVisible && chatText.Text == "新会话草稿";
            DismissChatInput(false);
            ReceivePreferences(PreferenceSnapshot("chat-B", 1000, 1000, 5000, 20, "basic"));
            bool noAutoOpen = !chatVisible;
            bool noPlaybackChange = playback.Active && reply == "聊天也不打断气泡" && currentCommandId == "chat-does-not-change-playback";
            bool textBounds = WellFormedChat("中文\r\n\t\U0001F40B") && !WellFormedChat("\ud800") && !WellFormedChat("\u0000");
            OpenChatInput(); chatText.Text = new string('字', ChatTextLimit + 1); int beforeInvalid = requests.Count; SubmitChatInput();
            textBounds &= requests.Count == beforeInvalid;

            CharacterClickGesture gesture = new CharacterClickGesture(); Point p = new Point(50, 50);
            gesture.Down(p, true); bool triple = !gesture.Up(p, 100);
            gesture.Down(p, true); triple &= !gesture.Up(p, 200);
            gesture.Down(p, true); triple &= gesture.Up(p, 300);
            gesture.Down(p, true); gesture.Up(p, 400);
            gesture.Down(p, true); gesture.Move(new Point(100, 100)); gesture.Move(p);
            bool noDragClick = !gesture.Up(p, 500);
            gesture.Down(p, true); noDragClick &= !gesture.Up(p, 600);
            gesture.Down(p, false); noDragClick &= !gesture.Up(p, 650);
            gesture.Down(p, true); noDragClick &= !gesture.Up(p, 700);
            gesture.Down(p, true); noDragClick &= !gesture.Up(p, 5000);
            Rect normal = ChatPlacement(new Rect(100, 50, 420, 420), new Rect(0, 0, 1920, 1040), 380, 194);
            Rect edge = ChatPlacement(new Rect(-10, 950, 640, 640), new Rect(0, 0, 1280, 1000), 440, 194);
            Rect negative = ChatPlacement(new Rect(-1850, -30, 420, 420), new Rect(-1920, -100, 1920, 1080), 380, 194);
            Rect tiny = ChatPlacement(new Rect(0, 0, 640, 640), new Rect(0, 0, 300, 180), 440, 194);
            bool placement = normal.Top == 476 && normal.Left == 120 && new Rect(0, 0, 1280, 1000).Contains(edge) &&
                new Rect(-1920, -100, 1920, 1080).Contains(negative) && new Rect(0, 0, 300, 180).Contains(tiny);
            bool focusGuard = ShouldRestoreChatFocus(true, new IntPtr(2), new IntPtr(2), new IntPtr(1)) &&
                !ShouldRestoreChatFocus(true, new IntPtr(3), new IntPtr(2), new IntPtr(1)) &&
                !ShouldRestoreChatFocus(false, new IntPtr(2), new IntPtr(2), new IntPtr(1));
            DismissChatInput(true); ClearBubble();
            ReceivePreferences(new Dictionary<string, object> { { "bindingId", null } });
            chatRequestObserver = null;
            chatTestResults = new Dictionary<string, bool>
            {
                { "unbound", unbound }, { "bindingCloses", bindingCloses }, { "multiline", multiline }, { "imeSafe", imeSafe },
                { "sent", sent }, { "noDuplicate", noDuplicate }, { "sameIdRetry", sameIdRetry }, { "staleIgnored", staleIgnored },
                { "failureDraft", failureDraft }, { "freshId", freshId }, { "successCloses", successCloses }, { "switchClears", switchClears },
                { "noAutoOpen", noAutoOpen }, { "noPlaybackChange", noPlaybackChange }, { "textBounds", textBounds },
                { "tripleClick", triple }, { "dragNotClick", noDragClick }, { "placement", placement }, { "focusGuard", focusGuard }
            };
            return unbound && bindingCloses && multiline && imeSafe && sent && noDuplicate && sameIdRetry && staleIgnored &&
                failureDraft && freshId && successCloses && switchClears && noAutoOpen && noPlaybackChange && textBounds && triple && noDragClick && placement && focusGuard;
        }
    }
}
