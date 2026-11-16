using System;
using System.IO;
using System.Linq;
using System.Windows.Forms;
using Microsoft.Win32;

namespace TritonLauncher
{
    /// <summary>
    /// "Start on boot". Two per-user entries, no admin rights needed:
    ///  1. Winlogon Shell (HKCU): "explorer.exe,C:\TritonLauncher\TritonLauncher.exe" - Windows starts us together
    ///     with the shell, right after sign-in, before the normal startup apps.
    ///  2. Run key fallback with --startup: if the Shell entry already started us, that copy exits silently.
    /// Other entries in the Shell list (e.g. Zoapi's) are left untouched.
    /// </summary>
    static class Autostart
    {
        const string WinlogonKey = @"Software\Microsoft\Windows NT\CurrentVersion\Winlogon";
        const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        const string RunValue = "TritonLauncher";
        // Earlier names of this launcher: their entries are removed / replaced too.
        static readonly string[] LegacyRunValues = { "NovaLauncher", "NucLauncher" };
        static readonly string[] OurExeNames = { "TritonLauncher.exe", "NovaLauncher.exe", "NucLauncher.exe" };

        static string ExePath => Application.ExecutablePath;
        static string ShellEntry => ExePath.Contains(" ") ? "\"" + ExePath + "\"" : ExePath;
        static string RunCommand => "\"" + ExePath + "\" --startup";

        public static bool IsEnabled() => ShellEntries().Any(IsThisExe) || RunEntryIsOurs();

        /// <summary>
        /// TV mode: the Shell list holds this launcher but not explorer.exe, so after sign-in Windows starts only
        /// Triton (no desktop, taskbar, Start menu, tray or Run-key startup apps). Takes effect at the next sign-in.
        /// </summary>
        public static bool TvModeIsSet()
        {
            var entries = ShellEntries();
            return entries.Any(IsThisExe) && !entries.Any(IsExplorer);
        }

        /// <summary>Is this sign-in running without the Windows desktop (no taskbar = explorer isn't the shell)?</summary>
        public static bool InTvSession() => FindWindow("Shell_TrayWnd", null) == IntPtr.Zero;

        public static void SetTvMode(bool on)
        {
            var entries = ShellEntries().Where(e => !IsOurs(e) && !IsExplorer(e)).ToList();
            if (!on) entries.Insert(0, "explorer.exe");
            entries.Add(ShellEntry);
            using (var key = Registry.CurrentUser.CreateSubKey(WinlogonKey))
                key.SetValue("Shell", string.Join(",", entries));
            using (var run = Registry.CurrentUser.CreateSubKey(RunKey))
                run.SetValue(RunValue, RunCommand);   // on the Windows desktop the launcher still opens (fallback)
        }

        [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
        static extern IntPtr FindWindow(string className, string windowName);

        static bool IsExplorer(string entry) =>
            string.Equals(Path.GetFileName(ExeOf(entry)), "explorer.exe", StringComparison.OrdinalIgnoreCase);

        public static void Set(bool enabled)
        {
            SetShellEntry(enabled);
            using (var run = Registry.CurrentUser.CreateSubKey(RunKey))
            {
                foreach (string legacy in LegacyRunValues) run.DeleteValue(legacy, false);
                if (enabled) run.SetValue(RunValue, RunCommand);
                else run.DeleteValue(RunValue, false);
            }
        }

        static void SetShellEntry(bool enabled)
        {
            // Start from the user's own Shell list, else the machine default (normally "explorer.exe").
            bool keepTvMode = enabled && TvModeIsSet();
            var entries = ShellEntries().Where(e => !IsOurs(e)).ToList();
            // Never a Shell list without the desktop unless TV mode is wanted: turning "Start on boot" off also turns
            // TV mode off, or the next sign-in would start nothing at all (a black screen).
            if (!keepTvMode && !entries.Any(IsExplorer)) entries.Insert(0, "explorer.exe");
            if (enabled) entries.Add(ShellEntry);

            using (var key = Registry.CurrentUser.CreateSubKey(WinlogonKey))
            {
                bool justExplorer = entries.Count == 1 && entries[0].Equals("explorer.exe", StringComparison.OrdinalIgnoreCase);
                if (justExplorer && !HasMachineOverride())
                    key.DeleteValue("Shell", false);   // back to the Windows default
                else
                    key.SetValue("Shell", string.Join(",", entries));
            }
        }

        static string[] ShellEntries()
        {
            string value;
            using (var user = Registry.CurrentUser.OpenSubKey(WinlogonKey))
                value = user?.GetValue("Shell") as string;
            if (string.IsNullOrWhiteSpace(value))
                using (var machine = Registry.LocalMachine.OpenSubKey(WinlogonKey))
                    value = machine?.GetValue("Shell") as string;
            return (value ?? "explorer.exe")
                .Split(',')
                .Select(e => e.Trim())
                .Where(e => e.Length > 0)
                .ToArray();
        }

        static bool HasMachineOverride()
        {
            using (var machine = Registry.LocalMachine.OpenSubKey(WinlogonKey))
            {
                var value = machine?.GetValue("Shell") as string;
                return value != null && !value.Trim().Equals("explorer.exe", StringComparison.OrdinalIgnoreCase);
            }
        }

        static string ExeOf(string entry)
        {
            string path = entry.Trim().Trim('"');
            int exe = path.IndexOf(".exe", StringComparison.OrdinalIgnoreCase);
            return exe >= 0 ? path.Substring(0, exe + 4) : path;
        }

        /// <summary>Any copy of this launcher (old name or another folder) - replaced when re-enabling.</summary>
        static bool IsOurs(string entry) =>
            OurExeNames.Any(n => string.Equals(Path.GetFileName(ExeOf(entry)), n, StringComparison.OrdinalIgnoreCase));

        static bool IsThisExe(string entry) =>
            string.Equals(ExeOf(entry), ExePath, StringComparison.OrdinalIgnoreCase);

        static bool RunEntryIsOurs()
        {
            using (var run = Registry.CurrentUser.OpenSubKey(RunKey))
                return run?.GetValue(RunValue) is string value && string.Equals(ExeOf(value), ExePath, StringComparison.OrdinalIgnoreCase);
        }
    }
}
