using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

namespace TritonLauncher
{
    /// <summary>
    /// Triton's voice assistant ("Hey Triton"): voice\TritonVoice.exe, packaged from the Voice Assistant project.
    /// Started together with the launcher and stopped when it exits; it sends its commands back through CommandPipe.
    /// </summary>
    static class VoiceAssistant
    {
        const string ProcessName = "TritonVoice";
        const string QuitEventName = "TritonVoice.Quit";   // the assistant waits on this to shut down tidily (tray icon removed)

        static string ExePath(string appDir) => Path.Combine(appDir, "voice", ProcessName + ".exe");

        public static bool IsInstalled(string appDir) => File.Exists(ExePath(appDir));

        public static bool IsRunning()
        {
            var running = Process.GetProcessesByName(ProcessName);
            foreach (var p in running) p.Dispose();
            return running.Length > 0;
        }

        public static void Start(string appDir)
        {
            string exe = ExePath(appDir);
            if (!File.Exists(exe) || IsRunning()) return;
            try
            {
                Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = Path.GetDirectoryName(exe), UseShellExecute = false })?.Dispose();
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
                    try { if (!p.WaitForExit(3000)) p.Kill(); }
                    catch { }
                }
            }
        }
    }
}
