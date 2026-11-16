using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace TritonLauncher
{
    /// <summary>
    /// The app windows Alt+Tab would show (minimized ones included), minus the launcher itself,
    /// so the "Open apps" tile can bring back anything that went behind or was minimized.
    /// </summary>
    static class OpenWindows
    {
        public class Item
        {
            public string id { get; set; }      // window handle, as text (JSON numbers lose precision)
            public string title { get; set; }
            public string app { get; set; }
            public string icon { get; set; }    // data: URL, or null
            public bool minimized { get; set; }
        }

        const int GW_OWNER = 4, GWL_EXSTYLE = -20, DWMWA_CLOAKED = 14;
        const int WS_EX_TOOLWINDOW = 0x80, WS_EX_APPWINDOW = 0x40000, WS_EX_NOACTIVATE = 0x08000000;
        const int SW_RESTORE = 9, SW_SHOW = 5;
        const uint WM_GETICON = 0x7F, SMTO_ABORTIFHUNG = 0x2, PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
        const int GCLP_HICON = -14, IconSize = 96;
        static readonly string[] ShellClasses = { "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd" };
        static readonly Dictionary<string, string> IconCache = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
        [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
        [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hWnd);
        [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
        [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hWnd);
        [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hWnd, int cmd);
        [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int index);
        [DllImport("user32.dll", EntryPoint = "GetClassLongPtrW")] static extern IntPtr GetClassLongPtr(IntPtr hWnd, int index);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int max);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr hWnd, StringBuilder text, int max);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hWnd, int cmd);
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);
        [DllImport("user32.dll")] static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam, uint flags, uint timeout, out IntPtr result);
        [DllImport("user32.dll")] static extern bool DestroyIcon(IntPtr hIcon);
        [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hWnd, int attr, out int value, int size);
        [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder path, ref int size);
        [DllImport("shell32.dll", CharSet = CharSet.Unicode)] static extern int SHDefExtractIcon(string file, int index, uint flags, out IntPtr large, out IntPtr small, uint sizes);

        static List<IntPtr> Handles()
        {
            uint self = (uint)Process.GetCurrentProcess().Id;
            var found = new List<IntPtr>();
            EnumWindows((h, _) =>
            {
                if (IsSwitchable(h, self)) found.Add(h);
                return true;
            }, IntPtr.Zero);
            return found;   // top of the z-order first, i.e. most recently used first
        }

        static bool IsSwitchable(IntPtr h, uint self)
        {
            if (!IsWindowVisible(h) || GetWindow(h, GW_OWNER) != IntPtr.Zero) return false;
            long ex = GetWindowLongPtr(h, GWL_EXSTYLE).ToInt64();
            if ((ex & WS_EX_APPWINDOW) == 0 && (ex & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)) != 0) return false;
            // Cloaked = hidden by Windows (suspended store apps, other virtual desktops)
            if (DwmGetWindowAttribute(h, DWMWA_CLOAKED, out int cloaked, 4) == 0 && cloaked != 0) return false;
            if (Text(h, GetWindowText).Length == 0 || Array.IndexOf(ShellClasses, Text(h, GetClassName)) >= 0) return false;
            GetWindowThreadProcessId(h, out uint pid);
            return pid != self;
        }

        public static int Count() => Handles().Count;

        public static List<Item> List()
        {
            var items = new List<Item>();
            foreach (var h in Handles())
            {
                GetWindowThreadProcessId(h, out uint pid);
                string exe = ExePath(pid);
                string title = Text(h, GetWindowText);
                // Store apps (Settings, Photos...) all run inside ApplicationFrameHost: their title is the app name
                bool frameHost = exe != null && Path.GetFileName(exe).Equals("ApplicationFrameHost.exe", StringComparison.OrdinalIgnoreCase);
                items.Add(new Item
                {
                    id = h.ToInt64().ToString(),
                    title = title,
                    app = frameHost ? title : AppName(exe),
                    icon = frameHost || exe == null ? WindowIcon(h) : ExeIcon(exe) ?? WindowIcon(h),
                    minimized = IsIconic(h),
                });
            }
            return items;
        }

        public static bool Activate(string id)
        {
            if (!long.TryParse(id, out long value)) return false;
            var h = new IntPtr(value);
            if (!IsWindow(h)) return false;
            ShowWindow(h, IsIconic(h) ? SW_RESTORE : SW_SHOW);
            return SetForegroundWindow(h);
        }

        // ---------- helpers ----------

        static string Text(IntPtr h, Func<IntPtr, StringBuilder, int, int> get)
        {
            var sb = new StringBuilder(256);
            get(h, sb, sb.Capacity);
            return sb.ToString().Trim();
        }

        static string ExePath(uint pid)
        {
            IntPtr process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
            if (process == IntPtr.Zero) return null;
            try
            {
                var sb = new StringBuilder(1024);
                int size = sb.Capacity;
                return QueryFullProcessImageName(process, 0, sb, ref size) ? sb.ToString() : null;
            }
            finally { CloseHandle(process); }
        }

        static string AppName(string exe)
        {
            if (exe == null) return "";
            if (Path.GetFileName(exe).Equals("explorer.exe", StringComparison.OrdinalIgnoreCase)) return "File Explorer";
            try
            {
                string description = FileVersionInfo.GetVersionInfo(exe).FileDescription;
                if (!string.IsNullOrWhiteSpace(description)) return description.Trim();
            }
            catch { }
            return Path.GetFileNameWithoutExtension(exe);
        }

        /// <summary>The program's own icon at a TV-friendly size, cached per exe.</summary>
        static string ExeIcon(string exe)
        {
            if (IconCache.TryGetValue(exe, out string cached)) return cached;
            string url = null;
            if (SHDefExtractIcon(exe, 0, 0, out IntPtr large, out IntPtr small, (uint)IconSize) == 0 && large != IntPtr.Zero)
            {
                url = ToDataUrl(large);
                DestroyIcon(large);
                if (small != IntPtr.Zero) DestroyIcon(small);
            }
            IconCache[exe] = url;
            return url;
        }

        /// <summary>The icon the window shows in its title bar (not ours to destroy).</summary>
        static string WindowIcon(IntPtr h)
        {
            SendMessageTimeout(h, WM_GETICON, (IntPtr)1 /* ICON_BIG */, IntPtr.Zero, SMTO_ABORTIFHUNG, 100, out IntPtr icon);
            if (icon == IntPtr.Zero) icon = GetClassLongPtr(h, GCLP_HICON);
            return icon == IntPtr.Zero ? null : ToDataUrl(icon);
        }

        static string ToDataUrl(IntPtr hIcon)
        {
            try
            {
                using (var icon = Icon.FromHandle(hIcon))
                using (var bitmap = icon.ToBitmap())
                using (var png = new MemoryStream())
                {
                    bitmap.Save(png, ImageFormat.Png);
                    return "data:image/png;base64," + Convert.ToBase64String(png.ToArray());
                }
            }
            catch { return null; }
        }
    }
}
