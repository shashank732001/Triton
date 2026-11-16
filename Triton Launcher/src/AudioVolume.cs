using System;
using System.Runtime.InteropServices;

namespace TritonLauncher
{
    /// <summary>Master volume of the default playback device via Windows Core Audio (no extra DLLs).</summary>
    static class AudioVolume
    {
        public class State
        {
            public int Level;
            public bool Muted;
        }

        public static State TryGet()
        {
            try
            {
                var ep = Endpoint();
                Marshal.ThrowExceptionForHR(ep.GetMasterVolumeLevelScalar(out float level));
                Marshal.ThrowExceptionForHR(ep.GetMute(out bool muted));
                Marshal.ReleaseComObject(ep);
                return new State { Level = (int)Math.Round(level * 100), Muted = muted };
            }
            catch
            {
                return null;   // no audio device (e.g. TV unplugged)
            }
        }

        public static void TrySetLevel(int level)
        {
            try
            {
                var ep = Endpoint();
                var ctx = Guid.Empty;
                ep.SetMasterVolumeLevelScalar(Math.Max(0, Math.Min(100, level)) / 100f, ref ctx);
                if (level > 0) ep.SetMute(false, ref ctx);
                Marshal.ReleaseComObject(ep);
            }
            catch { }
        }

        public static void TrySetMute(bool muted)
        {
            try
            {
                var ep = Endpoint();
                var ctx = Guid.Empty;
                ep.SetMute(muted, ref ctx);
                Marshal.ReleaseComObject(ep);
            }
            catch { }
        }

        public static void TryStep(int delta)
        {
            var current = TryGet();
            if (current != null) TrySetLevel(current.Level + delta);
        }

        static IAudioEndpointVolume Endpoint()
        {
            var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            try
            {
                Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(0 /* eRender */, 1 /* eMultimedia */, out IMMDevice device));
                var iid = typeof(IAudioEndpointVolume).GUID;
                Marshal.ThrowExceptionForHR(device.Activate(ref iid, 23 /* CLSCTX_ALL */, IntPtr.Zero, out object volume));
                Marshal.ReleaseComObject(device);
                return (IAudioEndpointVolume)volume;
            }
            finally
            {
                Marshal.ReleaseComObject(enumerator);
            }
        }

        [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
        class MMDeviceEnumerator { }

        [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IMMDeviceEnumerator
        {
            int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
            int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
        }

        [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IMMDevice
        {
            int Activate(ref Guid iid, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
        }

        // Vtable order matters: methods are declared exactly as in endpointvolume.h.
        [ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IAudioEndpointVolume
        {
            int RegisterControlChangeNotify(IntPtr notify);
            int UnregisterControlChangeNotify(IntPtr notify);
            int GetChannelCount(out uint count);
            int SetMasterVolumeLevel(float levelDb, ref Guid eventContext);
            int SetMasterVolumeLevelScalar(float level, ref Guid eventContext);
            int GetMasterVolumeLevel(out float levelDb);
            int GetMasterVolumeLevelScalar(out float level);
            int SetChannelVolumeLevel(uint channel, float levelDb, ref Guid eventContext);
            int SetChannelVolumeLevelScalar(uint channel, float level, ref Guid eventContext);
            int GetChannelVolumeLevel(uint channel, out float levelDb);
            int GetChannelVolumeLevelScalar(uint channel, out float level);
            int SetMute([MarshalAs(UnmanagedType.Bool)] bool muted, ref Guid eventContext);
            int GetMute([MarshalAs(UnmanagedType.Bool)] out bool muted);
        }
    }
}
