# Installs everything Triton Screen Share can use on this PC, in one go (run via Install-Everything.bat):
#   1. Firewall rule      - lets the other PC connect, on Public and Private Wi-Fi
#   2. Extend driver      - "Virtual Display Driver" (for Extend screen); from extend-driver\
#   3. Triton Camera    - the other PC's webcam as a camera here (Windows 11); TritonCamera.dll
#   4. VB-Audio Cable     - the other PC's microphone as a mic here; from vbcable\
# Anything already installed is skipped. -DryRun only prints what it would do. -Skip extend,camera,mic
# leaves those out (e.g. on a company laptop that only shares its screen). Needs administrator (it asks
# for it itself). Drivers 2 and 4 may show Windows' own "install this device software?" prompt.
param([switch]$DryRun, [switch]$Quiet, [string[]]$Skip = @())

$Skip = @($Skip | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })

$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $DryRun) {
  # Ask for administrator once, then run the rest in that window.
  $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
  if ($Quiet) { $argList += '-Quiet' }
  if ($Skip) { $argList += '-Skip'; $argList += ($Skip -join ',') }
  $p = Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -Wait -PassThru
  exit $(if ($p) { $p.ExitCode } else { 1 })   # no process = the administrator prompt was declined
}

# Default audio devices: read with the documented IMMDeviceEnumerator; set with IPolicyConfig, the
# (undocumented but long-stable) interface Windows' own Sound settings use.
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorCls {}
[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
  [PreserveSig] int EnumAudioEndpoints(int flow, int stateMask, out IMMDeviceCollection devices);
  [PreserveSig] int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice ep);
}
[ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceCollection { [PreserveSig] int GetCount(out int n); [PreserveSig] int Item(int i, out IMMDevice d); }
[ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore {
  int GetCount(out int n); int GetAt(int i, out PROPERTYKEY k);
  [PreserveSig] int GetValue(ref PROPERTYKEY k, IntPtr v);  // v: a 24-byte PROPVARIANT
  [PreserveSig] int SetValue(ref PROPERTYKEY k, IntPtr v);
  [PreserveSig] int Commit();
}
[StructLayout(LayoutKind.Sequential)] public struct PROPERTYKEY { public Guid fmtid; public int pid; }
[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
  int Activate();
  [PreserveSig] int OpenPropertyStore(int access, out IPropertyStore store);
  [PreserveSig] int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
}
[ComImport, Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")] class PolicyConfigCls {}
[ComImport, Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPolicyConfig {
  int A(); int B(); int C(); int D(); int E(); int F(); int G(); int H(); int I(); int J();
  [PreserveSig] int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string id, int role);
}
public static class LfrdAudio {
  public static string[] GetDefaults() {
    var e = (IMMDeviceEnumerator)new MMDeviceEnumeratorCls(); var r = new string[2];
    for (int flow = 0; flow < 2; ++flow) { IMMDevice d; if (e.GetDefaultAudioEndpoint(flow, 0, out d) == 0) d.GetId(out r[flow]); }
    return r;
  }
  // VB-CABLE's two ends get names that say what they are: the microphone apps pick ("Triton Mic") and
  // the end Triton Screen Share plays into ("Triton Mic Input"), plus the unused 16-channel end ("Triton
  // Spare 16 Ch"), so "CABLE" shows nowhere. Same as renaming in Sound settings. The
  // part Windows shows in brackets ("VB-Audio Virtual Cable") belongs to the driver: writing it is
  // denied even to administrators, so it stays.
  static readonly Guid Desc = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0");   // endpoint name (pid 2)
  static readonly Guid Iface = new Guid("026e516e-b814-414b-83cd-856d6fef4822");  // bracketed name (pid 2)
  // PROPVARIANT is 24 bytes (vt at 0, the string pointer at 8); a smaller C# struct reads as empty.
  static string Get(IPropertyStore s, Guid g) {
    var k = new PROPERTYKEY { fmtid = g, pid = 2 }; IntPtr v = Marshal.AllocHGlobal(24);
    try {
      for (int b = 0; b < 24; ++b) Marshal.WriteByte(v, b, 0);
      if (s.GetValue(ref k, v) != 0 || Marshal.ReadInt16(v) != 31) return "";
      return Marshal.PtrToStringUni(Marshal.ReadIntPtr(v, 8));
    } finally { Marshal.FreeHGlobal(v); }
  }
  static int Set(IPropertyStore s, Guid g, string value) {
    var k = new PROPERTYKEY { fmtid = g, pid = 2 }; IntPtr v = Marshal.AllocHGlobal(24), str = Marshal.StringToCoTaskMemUni(value);
    try {
      for (int b = 0; b < 24; ++b) Marshal.WriteByte(v, b, 0);
      Marshal.WriteInt16(v, 31); Marshal.WriteIntPtr(v, 8, str);
      return s.SetValue(ref k, v);
    } finally { Marshal.FreeCoTaskMem(str); Marshal.FreeHGlobal(v); }
  }
  public static string RenameCable() {
    var e = (IMMDeviceEnumerator)new MMDeviceEnumeratorCls(); var done = "";
    for (int flow = 0; flow < 2; ++flow) {  // 0 = playback ends, 1 = microphones
      IMMDeviceCollection c; if (e.EnumAudioEndpoints(flow, 1, out c) != 0) continue;
      int n; c.GetCount(out n);
      for (int i = 0; i < n; ++i) {
        IMMDevice d; c.Item(i, out d); IPropertyStore s;
        int hr = d.OpenPropertyStore(2 /* read+write */, out s);
        if (hr != 0) { done += "no write access (0x" + hr.ToString("X8") + "); "; continue; }
        string bracket = Get(s, Iface), name = Get(s, Desc);
        if (bracket.IndexOf("VB-Audio Virtual Cable") < 0) continue;
        string target = flow == 1 ? (name == "CABLE Output" || name == "Triton Mic" ? "Triton Mic" : null)
                                  : (name == "CABLE Input" || name == "Triton Mic Input" ? "Triton Mic Input"
                                     // the cable's unused 16-channel end; its name must not contain
                                     // "Triton Mic Input", which the sender looks for
                                     : name.StartsWith("CABLE In 16") || name == "Triton Spare 16 Ch" ? "Triton Spare 16 Ch" : null);
        if (target == null) continue;
        if (name == target) { done += target + " (already); "; continue; }
        hr = Set(s, Desc, target); if (hr == 0) hr = s.Commit();
        done += hr == 0 ? target + "; " : "could not rename " + name + " (0x" + hr.ToString("X8") + "); ";
      }
    }
    return done;
  }
  public static void SetDefault(string id) {
    if (string.IsNullOrEmpty(id)) return;
    var p = (IPolicyConfig)new PolicyConfigCls();
    for (int role = 0; role < 3; ++role) p.SetDefaultEndpoint(id, role);  // console, multimedia, communications
  }
}
'@

$results = [ordered]@{}
$rebootNeeded = $false
function Step($name) { Write-Host ''; Write-Host "== $name" -ForegroundColor Cyan }
function Done($name, $what) { $results[$name] = $what; Write-Host "   $what" }
function Run($what, [scriptblock]$action) {
  if ($DryRun) { Write-Host "   (dry run) would: $what" -ForegroundColor DarkGray; return 0 }
  $global:LASTEXITCODE = 0
  & $action
  return $global:LASTEXITCODE
}

Write-Host 'Triton Screen Share - install everything' -ForegroundColor White
if ($DryRun) { Write-Host '(dry run: nothing will be changed)' -ForegroundColor Yellow }

# 0. Old names -------------------------------------------------------------------------------------
# What this app (and Nova Launcher, now Triton Launcher) set up while called Lag-Free Display: firewall
# rules, and the camera DLL registered from ProgramData\LagFreeDisplay (same camera id as Triton Camera).
Step 'Leftovers of Lag-Free Display / Nova Launcher'
$old = 0
$oldRules = @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
              Where-Object Program -match '(?i)\\(LagFreeDisplay|NovaLauncher|NovaVoice)\.exe$' | Get-NetFirewallRule -ErrorAction SilentlyContinue) +
            @(Get-NetFirewallRule -DisplayName 'Lag-Free*', 'LagFree*', 'Nova Launcher*', 'NovaLauncher*', 'NovaVoice*' -ErrorAction SilentlyContinue) |
            Sort-Object Name -Unique
foreach ($r in $oldRules) {
  Run "remove firewall rule '$($r.DisplayName)'" { Remove-NetFirewallRule -Name $r.Name -ErrorAction SilentlyContinue } | Out-Null
  $old++
}
$oldCam = Join-Path $env:ProgramData 'LagFreeDisplay'
if (Test-Path $oldCam) {
  Run "unregister and delete $oldCam\LagFreeCamera.dll" {
    $d = Join-Path $oldCam 'LagFreeCamera.dll'
    if (Test-Path $d) { & regsvr32.exe /s /u $d }
    Remove-Item $oldCam -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $oldCam) {  # still loaded by Windows' camera service: stop it and retry
      Stop-Service FrameServer, FrameServerMonitor -Force -ErrorAction SilentlyContinue
      Start-Sleep -Milliseconds 500
      Remove-Item $oldCam -Recurse -Force -ErrorAction SilentlyContinue
    }
    $global:LASTEXITCODE = 0
  } | Out-Null
  $old++
}
Done 'Old names' $(if ($old -eq 0) { 'nothing left over' } elseif ($DryRun) { "would remove $old item(s)" } else { "removed $old item(s)" })

# 1. Firewall --------------------------------------------------------------------------------------
Step 'Firewall'
$exe = Join-Path $here 'TritonScreenShare.exe'
Run "replace all firewall rules for $exe with one inbound allow rule (all profiles)" {
  netsh advfirewall firewall delete rule name=all program="$exe" | Out-Null
  netsh advfirewall firewall add rule name="Triton Screen Share" dir=in action=allow program="$exe" enable=yes profile=any | Out-Null
} | Out-Null
Done 'Firewall' $(if ($DryRun) { 'would allow it on Public and Private networks' } else { 'allowed on Public and Private networks' })

# 2. Extend driver ---------------------------------------------------------------------------------
Step 'Extend screen (Virtual Display Driver)'
$vdd = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object FriendlyName -eq 'Virtual Display Driver'
if ($Skip -contains 'extend') {
  Done 'Extend driver' 'left out (-Skip extend)'
} elseif ($vdd) {
  Done 'Extend driver' 'already installed - skipped'
} else {
  $pkg = Join-Path $here 'extend-driver'
  # The package's "x86" folder holds the 64-bit build; ARM64 PCs use the ARM64 one.
  $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'ARM64' } else { 'x86' }
  $inf = Join-Path $pkg "SignedDrivers\$arch\VDD\MttVDD.inf"
  $devcon = Join-Path $pkg 'Dependencies\devcon.exe'
  if (-not (Test-Path $inf) -or -not (Test-Path $devcon)) {
    Done 'Extend driver' 'NOT installed: the extend-driver folder is missing from this copy'
  } else {
    # Its settings (screen sizes); the app adds the other PC's size here when needed.
    $settings = 'C:\VirtualDisplayDriver\vdd_settings.xml'
    if (-not (Test-Path $settings)) {
      Run "create $settings" {
        New-Item -ItemType Directory -Force 'C:\VirtualDisplayDriver' | Out-Null
        Copy-Item (Join-Path $pkg 'Dependencies\vdd_settings.xml') $settings
      } | Out-Null
    }
    $code = Run "install the driver with devcon (Root\MttVDD)" { & $devcon install $inf 'Root\MttVDD' | Out-Host }
    if ($DryRun) { Done 'Extend driver' 'would be installed' }
    elseif ($code -eq 0 -or $code -eq 1) { Done 'Extend driver' 'installed'; if ($code -eq 1) { $rebootNeeded = $true } }
    else { Done 'Extend driver' "NOT installed (devcon exit code $code)" }
  }
}

# 3. Camera ----------------------------------------------------------------------------------------
Step 'Triton Camera'
$build = [int](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').CurrentBuild
$dll = Join-Path $here 'TritonCamera.dll'
$dest = Join-Path $env:ProgramData 'TritonScreenShare\TritonCamera.dll'
$clsid = 'HKLM:\Software\Classes\CLSID\{7B2E6A1C-3F4D-4E8B-9A5C-2D1E0F6B8C47}\InprocServer32'
if ($Skip -contains 'camera') {
  Done 'Camera' 'left out (-Skip camera)'
} elseif ($build -lt 22000) {
  Done 'Camera' 'skipped: needs Windows 11 (this PC can still SHOW and share its own camera)'
} elseif (-not (Test-Path $dll)) {
  Done 'Camera' 'NOT installed: TritonCamera.dll is missing from this copy'
} elseif ((Test-Path $clsid) -and (Test-Path $dest) -and
          (Get-FileHash $dll).Hash -eq (Get-FileHash $dest).Hash) {
  Done 'Camera' 'already installed (same version) - skipped'
} else {
  $code = Run "copy TritonCamera.dll to $dest and register it (regsvr32)" {
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    if (Test-Path $dest) { & regsvr32.exe /s /u $dest; Start-Sleep -Milliseconds 300 }
    Copy-Item -Force $dll $dest -ErrorAction Stop
    & regsvr32.exe /s $dest
  }
  if ($DryRun) { Done 'Camera' 'would be installed' }
  elseif ($code -eq 0 -and (Test-Path $clsid)) { Done 'Camera' 'installed' }
  else { Done 'Camera' 'NOT installed (close apps using "Triton Camera", or restart, and run this again)' }
}

# 4. Microphone (VB-Audio Virtual Cable) ----------------------------------------------------------
Step 'Microphone (VB-Audio Virtual Cable)'
$cable = Get-PnpDevice -Class MEDIA -PresentOnly -ErrorAction SilentlyContinue | Where-Object FriendlyName -match 'VB-Audio Virtual Cable'
$setup = Join-Path $here 'vbcable\VBCABLE_Setup_x64.exe'
if ($Skip -contains 'mic') {
  Done 'Microphone' 'left out (-Skip mic)'
} elseif ($cable) {
  Done 'Microphone' 'already installed - skipped'
} elseif (-not (Test-Path $setup)) {
  Done 'Microphone' 'NOT installed: the vbcable folder is missing from this copy'
} else {
  Write-Host '   VB-CABLE is donationware by VB-Audio (Vincent Burel). Installing it means you accept'
  Write-Host '   its licence: https://vb-audio.com/Services/licensing.htm (also in vbcable\readme.txt).'
  $ok = $DryRun -or $Quiet
  if (-not $ok) { $ok = (Read-Host '   Install VB-CABLE? [Y/N]') -match '^[Yy]' }
  if (-not $ok) {
    Done 'Microphone' 'skipped (VB-CABLE licence not accepted)'
  } else {
    # VB-CABLE makes itself the default speaker, which silences the real one (and would make
    # Triton Screen Share share silence): remember the defaults and put them back afterwards.
    $before = if (-not $DryRun) { [LfrdAudio]::GetDefaults() } else { $null }
    $code = Run 'run VBCABLE_Setup_x64.exe -i -h (silent install), then restore the default speaker/mic' {
      $p = Start-Process $setup -ArgumentList '-i', '-h' -Wait -PassThru
      $global:LASTEXITCODE = $p.ExitCode
      if ($before) {
        Start-Sleep -Seconds 2
        [LfrdAudio]::SetDefault($before[0]); [LfrdAudio]::SetDefault($before[1])
        Write-Host "   Default speaker and microphone left as they were."
      }
    }
    if ($DryRun) { Done 'Microphone' 'would be installed (then a restart)' }
    else { Done 'Microphone' 'installed - RESTART this PC to finish'; $rebootNeeded = $true }
  }
}

# 5. Names -----------------------------------------------------------------------------------------
if ($DryRun -or (Get-PnpDevice -Class MEDIA -PresentOnly -ErrorAction SilentlyContinue | Where-Object FriendlyName -match 'VB-Audio Virtual Cable')) {
  Step 'Names'
  if ($DryRun) { Done 'Mic names' 'would name the cable "Triton Mic" / "Triton Mic Input" / "Triton Spare 16 Ch"' }
  else {
    $renamed = [LfrdAudio]::RenameCable()
    Done 'Mic names' $(if ($renamed) { "named: $renamed" } else { 'nothing to rename' })
  }
}

# Summary ------------------------------------------------------------------------------------------
Write-Host ''
Write-Host '== Summary' -ForegroundColor Cyan
foreach ($k in $results.Keys) { Write-Host ('   {0,-14} {1}' -f $k, $results[$k]) }
if ($rebootNeeded) { Write-Host ''; Write-Host '   Restart this PC once to finish installing.' -ForegroundColor Yellow }
if (-not $Quiet -and -not $DryRun) { Write-Host ''; Read-Host 'Press Enter to close' | Out-Null }
# 3010 = "done, restart needed" (the Windows installer convention), so a calling script can restart.
exit $(if ($rebootNeeded) { 3010 } else { 0 })
