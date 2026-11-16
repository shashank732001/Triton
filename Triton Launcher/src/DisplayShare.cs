using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

namespace TritonLauncher
{
    /// <summary>
    /// Triton Screen Share (screen sharing to / from this PC): display\TritonScreenShare.exe, copied from the
    /// "Triton Screen Share" project. Started with the launcher as just its always-on-top bubble, and
    /// closed when the launcher exits.
    /// </summary>
    static class DisplayShare
    {
        const string ProcessName = "TritonScreenShare";
        const string QuitEventName = "TritonScreenShare.Quit";   // it waits on this to stop sharing and close tidily

        static string ExePath(string appDir) => Path.Combine(appDir, "display", ProcessName + ".exe");

        public static void Start(string appDir)
        {
            string exe = ExePath(appDir);
            if (!File.Exists(exe)) return;
            try
            {
                // --background: bubble only, no panel popping up at sign-in. A copy already running just ignores it.
                Process.Start(new ProcessStartInfo(exe, "--background") { WorkingDirectory = Path.GetDirectoryName(exe), UseShellExecute = false })?.Dispose();
            }
            catch { /* blocked or broken install: the launcher works without it */ }
        }

        public static void Stop()
        {
            if (EventWaitHandle.TryOpenExisting(QuitEventName, out EventWaitHandle quit))
                using (quit) quit.Set();

            foreach (var p in Process.GetProcessesByName(ProcessName))
            {
                using (p)
                {
                    try { if (!p.WaitForExit(4000)) p.Kill(); }
                    catch { }
                }
            }
        }
    }
}
