using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Microsoft.Win32;

namespace TritonLauncher
{
    /// <summary>
    /// Borderless fullscreen window hosting the HTML home screen in WebView2.
    /// The page asks for things by id ("launch chrome"); only this class turns ids into processes.
    /// </summary>
    public class MainForm : Form
    {
        const string HostName = "launcher.local";
        const int HotkeyId = 1;
        const int WM_HOTKEY = 0x0312;
        const int WM_SYSCOMMAND = 0x0112, SC_MINIMIZE = 0xF020;
        const uint MOD_ALT = 0x1, MOD_CONTROL = 0x2, MOD_NOREPEAT = 0x4000;

        // Same colour as the page's loading screen, so the hand-over from window to page is seamless.
        static readonly Color BootBackground = Color.FromArgb(5, 6, 13);

        static readonly string[] WallpaperExtensions = { ".mp4", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".gif" };

        // Settings pages the status bar can open without being listed as tiles.
        static readonly Dictionary<string, string> Builtins = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["wifi"] = "ms-availablenetworks:",
            ["sound"] = "ms-settings:sound",
            ["display"] = "ms-settings:display",
        };

        [DllImport("user32.dll")] static extern bool RegisterHotKey(IntPtr hWnd, int id, uint modifiers, uint vk);
        [DllImport("user32.dll")] static extern bool UnregisterHotKey(IntPtr hWnd, int id);
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);

        readonly string appDir;
        readonly Options options;
        readonly Config config;
        readonly WebView2 web;
        readonly JavaScriptSerializer json = new JavaScriptSerializer();
        readonly Timer statusTimer = new Timer { Interval = 3000 };
        Wifi.Info wifi = Wifi.Info.Unknown;
        bool wifiRefreshing;
        int statusTicks;

        public MainForm(string appDir, Options options)
        {
            this.appDir = appDir;
            this.options = options;
            config = Config.Load(Path.Combine(appDir, "config.json"));

            Text = "Triton Launcher";
            BackColor = BootBackground;
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;   // a home screen, not an app: no taskbar button (clicking it only minimized the launcher)
            StartPosition = FormStartPosition.Manual;
            FitToScreen();

            web = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = BootBackground };
            Controls.Add(web);

            Load += async (s, e) => await InitWebViewAsync();
            Shown += (s, e) =>
            {
                BringHome();   // started by a background process (e.g. at sign-in): still come to the front
                if (options.ScreenshotPath == null)
                {
                    if (config.voiceAssistant) VoiceAssistant.Start(appDir);
                    if (config.screenShare) DisplayShare.Start(appDir);
                }
            };
            Activated += (s, e) =>
            {
                FitToScreen();
                Post(new { type = "visibility", visible = true });
            };
            Deactivate += (s, e) =>
            {
                // While another app is in front, stop covering the taskbar so it stays usable there.
                if (options.WindowSize.IsEmpty && WindowState != FormWindowState.Minimized)
                    Bounds = Screen.PrimaryScreen.WorkingArea;
                Post(new { type = "visibility", visible = false });
            };
            SystemEvents.DisplaySettingsChanged += OnDisplayChanged;
            statusTimer.Tick += (s, e) => OnStatusTick();
        }

        void FitToScreen()
        {
            Bounds = options.WindowSize.IsEmpty
                ? Screen.PrimaryScreen.Bounds
                : new Rectangle(Point.Empty, options.WindowSize);
        }

        void OnDisplayChanged(object sender, EventArgs e)
        {
            if (IsHandleCreated) BeginInvoke((Action)FitToScreen);
        }

        async Task InitWebViewAsync()
        {
            // Keep WebView2's profile out of the app folder so the launcher can live anywhere (even read-only).
            string dataDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "TritonLauncher", "WebView2");
            var envOptions = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required");
            var env = await CoreWebView2Environment.CreateAsync(null, dataDir, envOptions);
            await web.EnsureCoreWebView2Async(env);

            var core = web.CoreWebView2;
            core.SetVirtualHostNameToFolderMapping(HostName, appDir, CoreWebView2HostResourceAccessKind.Allow);

            var settings = core.Settings;
            settings.AreDevToolsEnabled = config.debug;
            settings.AreDefaultContextMenusEnabled = config.debug;
            settings.AreBrowserAcceleratorKeysEnabled = config.debug;
            settings.IsStatusBarEnabled = false;
            settings.IsZoomControlEnabled = false;
            settings.IsPinchZoomEnabled = false;
            settings.IsSwipeNavigationEnabled = false;

            // Weather uses the Windows location service; allow it without a browser-style prompt.
            core.PermissionRequested += (s, e) =>
            {
                if (e.PermissionKind == CoreWebView2PermissionKind.Geolocation) e.State = CoreWebView2PermissionState.Allow;
            };
            core.WebMessageReceived += OnWebMessage;
            core.NewWindowRequested += (s, e) => { e.Handled = true; StartProcess(e.Uri, ""); };
            core.NavigationCompleted += OnNavigationCompleted;

            CommandPipe.StartServer(line =>
            {
                if (IsHandleCreated && !IsDisposed) BeginInvoke((Action)(() => RunCommand(line)));
            });

            core.Navigate($"https://{HostName}/web/index.html");
            statusTimer.Start();
        }

        async void OnNavigationCompleted(object sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            if (options.Command != null) RunCommand(options.Command);
            if (options.ScreenshotPath == null) return;

            await Task.Delay(options.ScreenshotDelayMs);   // default 6 s: let wallpaper, fonts and weather settle
            using (var file = File.Create(options.ScreenshotPath))
                await web.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, file);
            Close();
        }

        // ---------- messages from the page ----------

        void OnWebMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            Dictionary<string, object> msg;
            try { msg = json.Deserialize<Dictionary<string, object>>(e.WebMessageAsJson); }
            catch { return; }

            switch (Str(msg, "cmd"))
            {
                case "hello":
                    Post(new { type = "init", wallpapers = ListWallpapers(), autostart = Autostart.IsEnabled(),
                               tvSession = Autostart.InTvSession(), tvSet = Autostart.TvModeIsSet() });
                    RefreshWifi();
                    PushStatus();
                    break;
                case "launch":
                    Launch(Str(msg, "id"));
                    break;
                case "volume":
                    AudioVolume.TrySetLevel(Int(msg, "level"));
                    PushStatus();
                    break;
                case "mute":
                    AudioVolume.TrySetMute(Bool(msg, "muted"));
                    PushStatus();
                    break;
                case "autostart":
                    Autostart.Set(Bool(msg, "enabled"));
                    PushStatus();
                    break;
                case "restartTo":
                    RestartTo(Str(msg, "mode") == "tv");
                    break;
                case "windows":
                    Post(new { type = "windows", items = OpenWindows.List() });
                    break;
                case "focusWindow":
                    if (!OpenWindows.Activate(Str(msg, "id")))
                        Post(new { type = "error", text = "That window was closed" });
                    break;
                case "voice":
                    // Mic button in the top bar: on/off until the launcher restarts (config.json decides the default)
                    if (Bool(msg, "enabled")) VoiceAssistant.Start(appDir);
                    else VoiceAssistant.Stop();
                    PushStatus();
                    break;
                case "exit":
                    ExitToWindows();
                    break;
                case "ready":
                    // Loading screen finished. Explorer may have grabbed the foreground while starting up at sign-in.
                    if ((DateTime.Now - Process.GetCurrentProcess().StartTime).TotalSeconds < 60) BringHome();
                    break;
            }
        }

        /// <summary>Text commands from the named pipe / --cmd: how the voice assistant opens apps and goes home.</summary>
        public void RunCommand(string text)
        {
            var parts = text.Trim().Split(new[] { ' ' }, 2, StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length == 0) return;
            string arg = parts.Length > 1 ? parts[1].Trim() : "";

            switch (parts[0].ToLowerInvariant())
            {
                case "show":
                case "home":
                    BringHome();
                    break;
                case "windows":
                case "apps":
                    BringHome();
                    Post(new { type = "showWindows" });
                    break;
                case "launch":
                case "open":
                    Launch(arg);
                    break;
                case "volume":
                    SetVolume(arg);
                    break;
                case "exit":
                    ExitToWindows();
                    break;
                case "restart-windows":
                    RestartTo(tvMode: false);
                    break;
                case "restart-tv":
                    RestartTo(tvMode: true);
                    break;
            }
        }

        // ---------- TV mode (the launcher instead of the Windows desktop) ----------

        /// <summary>
        /// On the Windows desktop: close the launcher. In TV mode there's no desktop behind it, so closing would leave
        /// a black screen: restart into Windows instead.
        /// </summary>
        void ExitToWindows()
        {
            if (Autostart.InTvSession()) RestartTo(tvMode: false);
            else Close();
        }

        /// <summary>Choose what the next sign-in starts (TV mode: only Triton; else the normal desktop), then restart.</summary>
        void RestartTo(bool tvMode)
        {
            try
            {
                Autostart.SetTvMode(tvMode);
            }
            catch (Exception e)
            {
                Post(new { type = "error", text = "Couldn't switch: " + e.Message });
                return;
            }
            Post(new { type = "restarting", text = tvMode ? "Restarting into TV mode…" : "Restarting to Windows…" });
            VoiceAssistant.Stop();
            DisplayShare.Stop();
            try
            {
                Process.Start(new ProcessStartInfo("shutdown.exe", "/r /t 3") { UseShellExecute = false, CreateNoWindow = true })?.Dispose();
            }
            catch (Exception e)
            {
                Post(new { type = "error", text = "Couldn't restart: " + e.Message });
            }
        }

        void SetVolume(string arg)
        {
            switch (arg.ToLowerInvariant())
            {
                case "up": AudioVolume.TryStep(+10); break;
                case "down": AudioVolume.TryStep(-10); break;
                case "mute": AudioVolume.TrySetMute(true); break;
                case "unmute": AudioVolume.TrySetMute(false); break;
                default:
                    if (int.TryParse(arg.TrimEnd('%'), out int level)) AudioVolume.TrySetLevel(level);
                    break;
            }
            PushStatus();
        }

        void Launch(string id)
        {
            var app = config.Find(id);
            if (app != null)
            {
                if (StartProcess(app.path, app.args))
                    Post(new { type = "launched", id = app.id, name = app.name });
                else
                    Post(new { type = "error", text = $"Couldn't open {app.name}. Check its path in config.json." });
            }
            else if (id != null && Builtins.TryGetValue(id, out string uri))
            {
                StartProcess(uri, "");
            }
            else
            {
                Post(new { type = "error", text = $"Unknown app \"{id}\"" });
            }
        }

        static bool StartProcess(string path, string args)
        {
            if (string.IsNullOrWhiteSpace(path)) return false;
            try
            {
                // ShellExecute resolves "chrome.exe"/"brave.exe" via App Paths and handles ms-settings: URIs.
                Process.Start(new ProcessStartInfo(Environment.ExpandEnvironmentVariables(path), args ?? "") { UseShellExecute = true });
                return true;
            }
            catch
            {
                return false;
            }
        }

        void BringHome()
        {
            if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
            Show();
            TopMost = true;   // nudge past the foreground lock, then behave like a normal window again
            TopMost = false;
            Activate();
            SetForegroundWindow(Handle);
            Post(new { type = "home" });
        }

        // ---------- status (Wi-Fi, volume) ----------

        void OnStatusTick()
        {
            if (++statusTicks % 3 == 0) RefreshWifi();   // netsh is slow-ish; every ~9 s is plenty
            PushStatus();
        }

        void RefreshWifi()
        {
            if (wifiRefreshing) return;
            wifiRefreshing = true;
            Task.Run(() => Wifi.Query()).ContinueWith(t =>
            {
                wifiRefreshing = false;
                if (t.Status == TaskStatus.RanToCompletion) wifi = t.Result;
                PushStatus();
            }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        void PushStatus()
        {
            var vol = AudioVolume.TryGet();
            Post(new
            {
                type = "status",
                wifi = new { connected = wifi.Connected, wired = wifi.Wired, ssid = wifi.Ssid, signal = wifi.Signal },
                volume = vol == null ? null : (object)new { level = vol.Level, muted = vol.Muted },
                autostart = Autostart.IsEnabled(),
                tvSet = Autostart.TvModeIsSet(),
                windows = OpenWindows.Count(),
                voice = VoiceAssistant.IsInstalled(appDir) ? (object)new { running = VoiceAssistant.IsRunning() } : null,
            });
        }

        string[] ListWallpapers()
        {
            string dir = Path.Combine(appDir, "wallpapers");
            if (!Directory.Exists(dir)) return new string[0];
            return Directory.GetFiles(dir)
                .Where(f => WallpaperExtensions.Contains(Path.GetExtension(f).ToLowerInvariant()))
                .OrderBy(f => f, StringComparer.OrdinalIgnoreCase)
                .Select(f => "wallpapers/" + Path.GetFileName(f))
                .ToArray();
        }

        void Post(object message)
        {
            if (web?.CoreWebView2 != null) web.CoreWebView2.PostWebMessageAsJson(json.Serialize(message));
        }

        static string Str(Dictionary<string, object> m, string key) =>
            m.TryGetValue(key, out object v) && v != null ? v.ToString() : null;

        static int Int(Dictionary<string, object> m, string key) =>
            m.TryGetValue(key, out object v) && v != null ? (int)Math.Round(Convert.ToDouble(v)) : 0;

        static bool Bool(Dictionary<string, object> m, string key) =>
            m.TryGetValue(key, out object v) && v is bool b && b;

        // ---------- Ctrl+Alt+Home brings the launcher back from anywhere ----------

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            RegisterHotKey(Handle, HotkeyId, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, (uint)Keys.Home);
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == WM_HOTKEY && m.WParam.ToInt32() == HotkeyId) BringHome();
            // With no taskbar button there'd be nothing to restore it from, so the launcher never minimizes.
            if (m.Msg == WM_SYSCOMMAND && (m.WParam.ToInt32() & 0xFFF0) == SC_MINIMIZE) return;
            base.WndProc(ref m);
        }

        protected override void OnFormClosed(FormClosedEventArgs e)
        {
            UnregisterHotKey(Handle, HotkeyId);
            SystemEvents.DisplaySettingsChanged -= OnDisplayChanged;
            statusTimer.Stop();
            VoiceAssistant.Stop();   // "Exit to Windows" leaves plain Windows, without "Hey Triton" listening
            DisplayShare.Stop();     // ...and without the screen-sharing bubble
            base.OnFormClosed(e);
        }
    }
}
