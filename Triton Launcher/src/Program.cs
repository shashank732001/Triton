using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

namespace TritonLauncher
{
    public class Options
    {
        public Size WindowSize = Size.Empty;   // --windowed 1920x1080 (testing); empty = fullscreen
        public string ScreenshotPath;          // --screenshot out.png (testing): capture and quit
        public int ScreenshotDelayMs = 6000;   // --screenshot-delay 800 (testing): when to capture
        public string Command;                 // --cmd launch chrome (forwarded to the running launcher)
        public bool Startup;                   // --startup: launched by the sign-in Run-key fallback
    }

    static class Program
    {
        [DllImport("user32.dll")]
        static extern bool AllowSetForegroundWindow(int processId);

        [STAThread]
        static void Main(string[] args)
        {
            if (args.Length > 0)
            {
                switch (args[0].ToLowerInvariant())
                {
                    case "--install": Autostart.Set(true); return;
                    case "--uninstall": Autostart.Set(false); return;
                    // TV mode on/off for the next sign-in. --windows is the way out if TV mode ever goes wrong:
                    // Ctrl+Alt+Del > Task Manager > Run new task > "...\TritonLauncher.exe" --windows
                    case "--tv-on": Autostart.SetTvMode(true); return;
                    case "--tv-off": Autostart.SetTvMode(false); return;
                    case "--windows":
                        Autostart.SetTvMode(false);
                        Process.Start(new ProcessStartInfo("shutdown.exe", "/r /t 0") { UseShellExecute = false, CreateNoWindow = true })?.Dispose();
                        return;
                }
            }

            var options = Parse(args);
            bool afterCrash = args.Any(a => a.Equals("--after-crash", StringComparison.OrdinalIgnoreCase));
            using (var mutex = new Mutex(true, "TritonLauncher.SingleInstance", out bool firstInstance))
            {
                // Restarted after a crash: the crashed copy may still be on its way out; wait for it instead of leaving
                if (!firstInstance && afterCrash)
                {
                    try { firstInstance = mutex.WaitOne(15000); }
                    catch (AbandonedMutexException) { firstInstance = true; }
                }
                if (!firstInstance)
                {
                    // Run-key fallback at sign-in, but the Winlogon Shell entry already started us: nothing to do.
                    if (options.Startup) return;
                    // Already running: hand our command (or "show") to it and leave.
                    AllowSetForegroundWindow(-1);
                    CommandPipe.Send(options.Command ?? "show");
                    return;
                }

                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                // In TV mode the launcher is the only thing on screen: if it crashes, start it again rather than leave
                // a black screen (Windows doesn't restart a replacement shell by itself)
                AppDomain.CurrentDomain.UnhandledException += (s, e) => RestartAfterCrash();
                Application.ThreadException += (s, e) => { RestartAfterCrash(); Environment.Exit(1); };
                Application.Run(new MainForm(AppDomain.CurrentDomain.BaseDirectory, options));
            }
        }

        static void RestartAfterCrash()
        {
            try
            {
                if (Autostart.InTvSession())
                    Process.Start(new ProcessStartInfo(Application.ExecutablePath, "--after-crash") { UseShellExecute = false })?.Dispose();
            }
            catch { }
        }

        static Options Parse(string[] args)
        {
            var o = new Options();
            for (int i = 0; i < args.Length; i++)
            {
                switch (args[i].ToLowerInvariant())
                {
                    case "--windowed" when i + 1 < args.Length:
                        var size = args[++i].Split('x');
                        o.WindowSize = new Size(int.Parse(size[0]), int.Parse(size[1]));
                        break;
                    case "--screenshot" when i + 1 < args.Length:
                        o.ScreenshotPath = Path.GetFullPath(args[++i]);
                        break;
                    case "--screenshot-delay" when i + 1 < args.Length:
                        o.ScreenshotDelayMs = int.Parse(args[++i]);
                        break;
                    case "--startup":
                        o.Startup = true;
                        break;
                    case "--cmd":
                        o.Command = string.Join(" ", args.Skip(i + 1));
                        i = args.Length;
                        break;
                }
            }
            return o;
        }
    }
}
