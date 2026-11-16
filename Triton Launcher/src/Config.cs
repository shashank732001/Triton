using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Web.Script.Serialization;

namespace TritonLauncher
{
    // Property names are lowercase to match config.json exactly.
    // The page reads the same file for the visual settings (city, wallpaper, ...); the host only needs these.
    public class AppEntry
    {
        public string id { get; set; }
        public string name { get; set; }
        public string path { get; set; }
        public string args { get; set; }
    }

    public class Config
    {
        public bool debug { get; set; }
        public bool voiceAssistant { get; set; } = true;   // start voice\TritonVoice.exe ("Hey Triton") with the launcher
        public bool screenShare { get; set; } = true;      // start display\TritonScreenShare.exe (the screen-sharing bubble) with the launcher
        public List<AppEntry> apps { get; set; } = new List<AppEntry>();

        public static Config Load(string file)
        {
            Config config = null;
            try { config = new JavaScriptSerializer().Deserialize<Config>(File.ReadAllText(file)); }
            catch { /* missing or malformed: fall back to defaults below */ }

            if (config?.apps == null || config.apps.Count == 0)
                config = new Config { debug = config?.debug ?? false, voiceAssistant = config?.voiceAssistant ?? true, screenShare = config?.screenShare ?? true, apps = Defaults() };
            return config;
        }

        /// <summary>Match by id or display name, so a voice command like "open chrome" works either way.</summary>
        public AppEntry Find(string idOrName)
        {
            if (string.IsNullOrWhiteSpace(idOrName)) return null;
            return apps.FirstOrDefault(a => string.Equals(a.id, idOrName, StringComparison.OrdinalIgnoreCase))
                ?? apps.FirstOrDefault(a => string.Equals(a.name, idOrName, StringComparison.OrdinalIgnoreCase));
        }

        static List<AppEntry> Defaults() => new List<AppEntry>
        {
            new AppEntry { id = "files", name = "Files", path = "explorer.exe" },
            new AppEntry { id = "chrome", name = "Chrome", path = "chrome.exe" },
            new AppEntry { id = "brave", name = "Brave", path = "brave.exe" },
            new AppEntry { id = "settings", name = "Settings", path = "ms-settings:" },
        };
    }
}
