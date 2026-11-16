using System;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Net.NetworkInformation;
using System.Text;

namespace TritonLauncher
{
    static class Wifi
    {
        public class Info
        {
            public bool Connected;
            public bool Wired;
            public string Ssid = "";
            public int Signal;

            public static readonly Info Unknown = new Info();
        }

        /// <summary>Reads the current Wi-Fi network from netsh; falls back to detecting a wired connection.</summary>
        public static Info Query()
        {
            var info = new Info();
            try
            {
                var psi = new ProcessStartInfo("netsh", "wlan show interfaces")
                {
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    CreateNoWindow = true,
                    StandardOutputEncoding = Encoding.GetEncoding(CultureInfo.CurrentCulture.TextInfo.OEMCodePage),
                };
                using (var p = Process.Start(psi))
                {
                    string output = p.StandardOutput.ReadToEnd();
                    p.WaitForExit(3000);
                    foreach (string line in output.Split('\n'))
                    {
                        int colon = line.IndexOf(':');
                        if (colon < 0) continue;
                        string key = line.Substring(0, colon).Trim();
                        string value = line.Substring(colon + 1).Trim();
                        // "SSID" is only listed while connected ("BSSID" is a different key).
                        if (key == "SSID" && value.Length > 0) { info.Ssid = value; info.Connected = true; }
                        else if (key == "Signal") int.TryParse(value.TrimEnd('%', ' '), out info.Signal);
                    }
                }
            }
            catch { /* no Wi-Fi adapter or netsh unavailable */ }

            if (!info.Connected)
            {
                info.Wired = NetworkInterface.GetAllNetworkInterfaces().Any(n =>
                    n.OperationalStatus == OperationalStatus.Up &&
                    n.NetworkInterfaceType == NetworkInterfaceType.Ethernet &&
                    n.GetIPProperties().GatewayAddresses.Count > 0);
                info.Connected = info.Wired;
            }
            return info;
        }
    }
}
