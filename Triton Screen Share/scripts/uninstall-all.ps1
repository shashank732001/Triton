# Removes everything Triton Screen Share (formerly Lag-Free Display) set up on this PC, so that its folder
# can simply be deleted afterwards (run via Uninstall-Everything.bat):
#   for you:   the running app, "Start with Windows", its settings (trusted PCs, Start as) and cache, and
#              Windows' records of it (mic/camera permissions, tray icon, program names, notifications)
#   this PC:   its firewall rules, Triton Camera, VB-Audio Virtual Cable ("Triton Mic") and the extend
#              driver (Virtual Display Driver) with its settings folder and certificate
# -KeepDrivers keeps VB-Cable and the extend driver (other apps may use them) and gives the cable back
# its own names. -DryRun only lists what it would remove. Asks for administrator once.
param([switch]$DryRun, [switch]$KeepDrivers, [switch]$MachineOnly, [switch]$Quiet)

$ErrorActionPreference = 'SilentlyContinue'
$here = $PSScriptRoot
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
$files = '(?i)(TritonScreenShare|LagFreeDisplay)\.exe|(TritonCamera|LagFreeCamera)\.dll'
$names = '(?i)TritonScreenShare|LagFreeDisplay'
$script:count = 0
$script:reboot = $false

function Step($name) { Write-Host ''; Write-Host "== $name" -ForegroundColor Cyan }
function Did($what) { $script:count++; Write-Host "   $(if ($DryRun) { 'would remove' } else { 'removed' }) $what" }
function Remove($path, $what) {
  if (Test-Path -LiteralPath $path) { if (-not $DryRun) { Remove-Item -LiteralPath $path -Recurse -Force }; Did $what }
}
function RemoveValues($key, $pattern, $what) {
  $item = Get-Item -LiteralPath $key
  if (-not $item) { return }
  foreach ($v in $item.GetValueNames() | Where-Object { $_ -match $pattern }) {
    if (-not $DryRun) { Remove-ItemProperty -LiteralPath $key -Name $v }
    Did "$what $v"
  }
}

Write-Host 'Triton Screen Share - uninstall everything' -ForegroundColor White
if ($DryRun) { Write-Host '(dry run: nothing will be changed)' -ForegroundColor Yellow }

# ==================================================================================================
# For you (this Windows account)
# ==================================================================================================
if (-not $MachineOnly) {
  Step 'The app'
  if (-not $DryRun) {  # close it tidily (it waits on this event), then whatever is left
    foreach ($e in 'TritonScreenShare.Quit', 'LagFreeDisplay.Quit') {
      $h = $null
      if ([Threading.EventWaitHandle]::TryOpenExisting($e, [ref]$h)) { $h.Set() | Out-Null; $h.Dispose() }
    }
  }
  foreach ($p in Get-Process TritonScreenShare, LagFreeDisplay) {
    if (-not $DryRun -and -not $p.WaitForExit(4000)) { $p.Kill(); $p.WaitForExit(2000) | Out-Null }
    Did "running $($p.ProcessName).exe (closed)"
  }

  Step 'Start with Windows, settings, cache'
  RemoveValues 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' $names 'startup entry'
  RemoveValues 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run' $names 'startup setting'
  Get-ChildItem ([Environment]::GetFolderPath('Startup')) -File | Where-Object Name -match '(?i)triton ?screen|lag-?free' |
    ForEach-Object { Remove $_.FullName "startup shortcut $($_.Name)" }
  Remove 'HKCU:\Software\TritonScreenShare' 'settings (Start as, trusted and known PCs)'
  Remove 'HKCU:\Software\LagFreeDisplay' 'settings from when it was called Lag-Free Display'
  Remove (Join-Path $env:LOCALAPPDATA 'TritonScreenShare') "cache ($env:LOCALAPPDATA\TritonScreenShare)"
  Remove (Join-Path $env:LOCALAPPDATA 'LagFreeDisplay') "old cache ($env:LOCALAPPDATA\LagFreeDisplay)"

  Step "Windows' records of the app"
  # Mic / camera / other permissions: one key per program path ("C:#...#TritonScreenShare.exe").
  Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore' |
    ForEach-Object { Get-ChildItem (Join-Path $_.PSPath 'NonPackaged') } |
    Where-Object PSChildName -match $files | ForEach-Object { Remove $_.PSPath "permission record $($_.PSChildName)" }
  # Tray icon (Windows 11 keeps one per program, with its path).
  Get-ChildItem 'HKCU:\Control Panel\NotifyIconSettings' | Where-Object { (Get-ItemProperty $_.PSPath).ExecutablePath -match $files } |
    ForEach-Object { Remove $_.PSPath "tray icon record for $((Get-ItemProperty $_.PSPath).ExecutablePath)" }
  # Program names, compatibility notes and usage counters: values named after the program's path.
  foreach ($key in 'HKCU:\Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache',
                   'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Compatibility Assistant\Store',
                   'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers',
                   'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FeatureUsage\AppSwitched',
                   'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FeatureUsage\ShowJumpView',
                   'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FeatureUsage\AppLaunch') {
    RemoveValues $key $files "Windows' record of"
  }
  Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings' |
    Where-Object PSChildName -match $names | ForEach-Object { Remove $_.PSPath "notification settings $($_.PSChildName)" }
}

# ==================================================================================================
# This PC (needs administrator)
# ==================================================================================================
if (-not $isAdmin -and -not $DryRun) {
  Write-Host ''
  Write-Host 'Asking for administrator to remove the firewall rules, camera and drivers...'
  $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-MachineOnly')
  if ($KeepDrivers) { $argList += '-KeepDrivers' }
  if ($Quiet) { $argList += '-Quiet' }
  $p = Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -Wait -PassThru
  if (-not $p) { Write-Host '   The administrator prompt was declined: firewall rules, camera and drivers are still there.' -ForegroundColor Yellow }
  if (-not $Quiet) { Write-Host ''; Read-Host 'Press Enter to close' | Out-Null }
  exit $(if ($p) { $p.ExitCode } else { 1 })
}

# Driver packages in Windows' driver store, by their original .inf name (e.g. oem305.inf).
function DriverPackages($inf) {
  if ($isAdmin) { return @(Get-WindowsDriver -Online | Where-Object OriginalFileName -match $inf | ForEach-Object { Split-Path $_.Driver -Leaf }) }
  $cur = $null  # dry run without administrator: read pnputil's list instead
  @(pnputil /enum-drivers | ForEach-Object {
    if ($_ -match ':\s+(oem\d+\.inf)\s*$') { $cur = $Matches[1] } elseif ($_ -match $inf -and $cur) { $cur }
  })
}
function RemoveDriverPackages($inf, $what) {
  foreach ($oem in DriverPackages $inf | Sort-Object -Unique) {
    if (-not $DryRun) { pnputil /delete-driver $oem /uninstall /force | Out-Null; $script:reboot = $true }
    Did "$what driver package ($oem)"
  }
}

Step 'Firewall'
$rules = @(Get-NetFirewallApplicationFilter | Where-Object Program -match $files | Get-NetFirewallRule) +
         @(Get-NetFirewallRule -DisplayName 'Triton Screen Share*', 'Lag-Free*', 'LagFree*') | Sort-Object Name -Unique
foreach ($r in $rules) {
  if (-not $DryRun) { Remove-NetFirewallRule -Name $r.Name }
  Did "firewall rule '$($r.DisplayName)' ($($r.Direction), $($r.Action))"
}

Step 'Triton Camera'
foreach ($dir in (Join-Path $env:ProgramData 'TritonScreenShare'), (Join-Path $env:ProgramData 'LagFreeDisplay')) {
  if (-not (Test-Path $dir)) { continue }
  if (-not $DryRun) {
    Get-ChildItem $dir -Filter *.dll | ForEach-Object { & regsvr32.exe /s /u $_.FullName }
    Start-Sleep -Milliseconds 300
    Remove-Item $dir -Recurse -Force
    if (Test-Path $dir) {  # still loaded by Windows' camera service: stop it and retry
      Stop-Service FrameServer, FrameServerMonitor -Force
      Start-Sleep -Milliseconds 500
      Remove-Item $dir -Recurse -Force
    }
    if (Test-Path $dir) { Write-Host "   could not delete $dir (in use): restart and run this again" -ForegroundColor Yellow }
  }
  Did "the camera (unregistered) and $dir"
}
Remove 'HKLM:\Software\Classes\CLSID\{7B2E6A1C-3F4D-4E8B-9A5C-2D1E0F6B8C47}' 'the camera registration'

if ($KeepDrivers) {
  Step 'Drivers kept (-KeepDrivers)'
  # Give VB-Cable's ends back their own names (the installer called them "Triton ...").
  Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class UnEnumCls {}
[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUnEnum { [PreserveSig] int EnumAudioEndpoints(int flow, int state, out IUnCol c); }
[ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUnCol { [PreserveSig] int GetCount(out int n); [PreserveSig] int Item(int i, out IUnDev d); }
[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUnDev { int Activate(); [PreserveSig] int OpenPropertyStore(int access, out IUnStore s); }
[ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUnStore {
  int GetCount(out int n); int GetAt(int i, out UnKey k);
  [PreserveSig] int GetValue(ref UnKey k, IntPtr v); [PreserveSig] int SetValue(ref UnKey k, IntPtr v); [PreserveSig] int Commit();
}
[StructLayout(LayoutKind.Sequential)] public struct UnKey { public Guid g; public int pid; }
public static class CableNames {
  // Endpoint name: PKEY_Device_DeviceDesc. PROPVARIANT is 24 bytes: vt at 0, the string at 8.
  public static string Restore(bool dryRun) {
    var e = (IUnEnum)new UnEnumCls(); var done = ""; var key = new UnKey { g = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"), pid = 2 };
    string[] from = { "Triton Mic", "Triton Mic Input", "Triton Spare 16 Ch" }, to = { "CABLE Output", "CABLE Input", "CABLE In 16ch" };
    for (int flow = 0; flow < 2; ++flow) {
      IUnCol c; if (e.EnumAudioEndpoints(flow, 1, out c) != 0) continue; int n; c.GetCount(out n);
      for (int i = 0; i < n; ++i) {
        IUnDev d; c.Item(i, out d); IUnStore s; if (d.OpenPropertyStore(dryRun ? 0 : 2, out s) != 0) continue;
        IntPtr v = Marshal.AllocHGlobal(24);
        try {
          for (int b = 0; b < 24; ++b) Marshal.WriteByte(v, b, 0);
          if (s.GetValue(ref key, v) != 0 || Marshal.ReadInt16(v) != 31) continue;
          int j = Array.IndexOf(from, Marshal.PtrToStringUni(Marshal.ReadIntPtr(v, 8)));
          if (j < 0) continue;
          if (!dryRun) {
            IntPtr str = Marshal.StringToCoTaskMemUni(to[j]);
            for (int b = 0; b < 24; ++b) Marshal.WriteByte(v, b, 0);
            Marshal.WriteInt16(v, 31); Marshal.WriteIntPtr(v, 8, str);
            int hr = s.SetValue(ref key, v); if (hr == 0) hr = s.Commit(); Marshal.FreeCoTaskMem(str);
            if (hr != 0) { done += "could not rename " + from[j] + "; "; continue; }
          }
          done += from[j] + " -> " + to[j] + "; ";
        } finally { Marshal.FreeHGlobal(v); }
      }
    }
    return done;
  }
}
'@
  $renamed = [CableNames]::Restore([bool]$DryRun)
  if ($renamed) { $script:count++; Write-Host "   $(if ($DryRun) { 'would rename' } else { 'renamed' }) $renamed" }
  Write-Host '   VB-Audio Virtual Cable and the Virtual Display Driver stay installed.'
} else {
  Step 'Microphone (VB-Audio Virtual Cable, "Triton Mic")'
  $cable = Get-PnpDevice -Class MEDIA | Where-Object FriendlyName -match 'VB-Audio Virtual Cable'
  $setup = Join-Path $here 'vbcable\VBCABLE_Setup_x64.exe'
  if ($cable -and (Test-Path $setup)) {
    if (-not $DryRun) { Start-Process $setup -ArgumentList '-u', '-h' -Wait; $script:reboot = $true }
    Did 'VB-Audio Virtual Cable (with its own uninstaller)'
  }
  RemoveDriverPackages 'vbmmecable' 'VB-Audio Virtual Cable'   # anything its uninstaller left (or without it)

  Step 'Extend screen (Virtual Display Driver)'
  foreach ($d in Get-PnpDevice | Where-Object { $_.HardwareID -contains 'Root\MttVDD' -or $_.FriendlyName -eq 'Virtual Display Driver' }) {
    if (-not $DryRun) { pnputil /remove-device $d.InstanceId | Out-Null; $script:reboot = $true }
    Did "the virtual screen device ($($d.InstanceId))"
  }
  RemoveDriverPackages 'mttvdd' 'Virtual Display Driver'
  Remove 'C:\VirtualDisplayDriver' 'its settings folder C:\VirtualDisplayDriver'
  # The driver's own installer (VDD Control) trusts its certificate machine-wide.
  foreach ($c in Get-ChildItem Cert:\LocalMachine\Root, Cert:\LocalMachine\TrustedPublisher | Where-Object Subject -eq 'CN=Virtual Display Driver') {
    if (-not $DryRun) { Remove-Item -LiteralPath $c.PSPath }
    Did "the trusted certificate 'CN=Virtual Display Driver' ($(Split-Path $c.PSParentPath -Leaf))"
  }
}

# Summary ------------------------------------------------------------------------------------------
Write-Host ''
if ($script:count -eq 0) { Write-Host '   Nothing was left on this PC.' }
elseif ($DryRun) { Write-Host "   Dry run: $($script:count) item(s) would be removed." }
else { Write-Host "   Done: $($script:count) item(s) removed." -ForegroundColor Green }
if ($script:reboot) { Write-Host '   Restart this PC once to finish removing the drivers.' -ForegroundColor Yellow }
if (-not $DryRun -and -not $MachineOnly) { Write-Host '   You can now delete the Triton Screen Share folder.' }
if (-not $Quiet -and -not $DryRun) { Write-Host ''; Read-Host 'Press Enter to close' | Out-Null }
exit 0
