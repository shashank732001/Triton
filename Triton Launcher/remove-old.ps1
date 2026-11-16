# Removes, for this user, what this launcher and its screen sharing left on this PC under their old names
# (Nova Launcher, NovaVoice, Lag-Free Display): running copies, startup entries, settings, caches, Windows'
# records of them (mic/camera permissions, tray icon, app names) and the old Nova Launcher folder.
# Install.cmd runs it before installing; display\install-all.ps1 (administrator) removes the machine-wide
# leftovers: firewall rules and the old camera DLL.   -DryRun only lists what it would remove.
param([switch]$DryRun)
$ErrorActionPreference = 'SilentlyContinue'
$here = $PSScriptRoot.TrimEnd('\')
$oldExe = '(?i)(NovaLauncher|NovaVoice|LagFreeDisplay)\.exe|LagFreeCamera\.dll'
$found = 0
function Did($what) { $script:found++; Write-Host "   $(if ($DryRun) { 'would remove' } else { 'removed' }) $what" }
function Remove($path, $what) {
  if (Test-Path $path) { if (-not $DryRun) { Remove-Item $path -Recurse -Force }; Did $what }
}

Write-Host 'Removing Nova Launcher / Lag-Free Display leftovers'

# 1. Where the old launcher lives (its startup entries and running copy say), before anything is removed.
$folders = @('C:\NovaLauncher')
$folders += Get-Process NovaLauncher | ForEach-Object { Split-Path $_.Path }
$run = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run').NovaLauncher
$shell = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Winlogon').Shell
foreach ($entry in @($run) + @("$shell" -split ',')) {
  if ($entry -match '^\s*"?(.+?NovaLauncher\.exe)') { $folders += Split-Path $Matches[1] }
}
$folders = $folders | Where-Object { $_ -and (Test-Path (Join-Path $_ 'NovaLauncher.exe')) } |
  ForEach-Object { (Resolve-Path $_).Path.TrimEnd('\') } | Sort-Object -Unique | Where-Object { $_ -ne $here }

# 2. Close the old apps: the assistant and screen sharing tidily (they wait on these events), then the rest.
if (-not $DryRun) {
  foreach ($e in 'NovaVoice.Quit', 'LagFreeDisplay.Quit') {
    $h = $null
    if ([Threading.EventWaitHandle]::TryOpenExisting($e, [ref]$h)) { $h.Set() | Out-Null; $h.Dispose() }
  }
}
foreach ($name in 'NovaLauncher', 'NovaVoice', 'LagFreeDisplay') {
  foreach ($p in Get-Process $name) {
    if (-not $DryRun -and -not $p.WaitForExit(4000)) { $p.Kill(); $p.WaitForExit(2000) | Out-Null }
    Did "running $name.exe (closed)"
  }
}

# 3. The old folder: keep your settings (tiles, wallpaper choice) and your own wallpapers, then delete it.
foreach ($old in $folders) {
  $cfg = Join-Path $old 'config.json'
  if ((Test-Path $cfg) -and -not $DryRun) { Copy-Item $cfg (Join-Path $here 'config.json') -Force }
  if (Test-Path (Join-Path $old 'wallpapers')) {
    Get-ChildItem (Join-Path $old 'wallpapers') -File | Where-Object { -not (Test-Path (Join-Path $here "wallpapers\$($_.Name)")) } |
      ForEach-Object { if (-not $DryRun) { Copy-Item $_.FullName (Join-Path $here 'wallpapers') } }
  }
  if (Test-Path $cfg) { Write-Host "   kept your config.json and wallpapers from $old" }
  Remove $old "the old launcher folder $old"
}

# 4. Startup entries (Install.cmd's --install rewrites the Shell entry without the old launcher next).
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
foreach ($v in 'NovaLauncher', 'NovaVoice', 'LagFreeDisplay') {
  if ($null -ne (Get-ItemProperty $runKey).$v) { if (-not $DryRun) { Remove-ItemProperty $runKey $v }; Did "startup entry $v" }
}
$startup = [Environment]::GetFolderPath('Startup')
Get-ChildItem $startup -File | Where-Object Name -match '(?i)nova|lag-?free' | ForEach-Object { Remove $_.FullName "startup shortcut $($_.Name)" }

# 5. Their own settings and caches.
Remove 'HKCU:\Software\LagFreeDisplay' 'Lag-Free Display settings (HKCU\Software\LagFreeDisplay)'
Remove (Join-Path $env:LOCALAPPDATA 'NovaLauncher') "Nova Launcher's browser cache ($env:LOCALAPPDATA\NovaLauncher)"
Remove (Join-Path $env:LOCALAPPDATA 'LagFreeDisplay') "Lag-Free Display's logs ($env:LOCALAPPDATA\LagFreeDisplay)"

# 6. What Windows recorded about the old programs.
#    Mic / camera / other permissions: one key per program path ("C:#NovaLauncher#voice#NovaVoice.exe").
foreach ($root in 'HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore') {
  Get-ChildItem $root | ForEach-Object { Get-ChildItem (Join-Path $_.PSPath 'NonPackaged') } |
    Where-Object PSChildName -match $oldExe | ForEach-Object { Remove $_.PSPath "permission record $($_.PSChildName)" }
}
#    Tray icons (Windows 11 keeps one per program, with its path).
Get-ChildItem 'HKCU:\Control Panel\NotifyIconSettings' | Where-Object { (Get-ItemProperty $_.PSPath).ExecutablePath -match $oldExe } |
  ForEach-Object { Remove $_.PSPath "tray icon record for $((Get-ItemProperty $_.PSPath).ExecutablePath)" }
#    Program names and compatibility notes: values named after the program's path.
foreach ($key in 'HKCU:\Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache',
                 'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Compatibility Assistant\Store',
                 'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers') {
  $item = Get-Item $key
  if (-not $item) { continue }
  foreach ($v in $item.GetValueNames() | Where-Object { $_ -match $oldExe }) {
    if (-not $DryRun) { Remove-ItemProperty $key -Name $v }
    Did "Windows' record of $v"
  }
}
#    Notification settings of the old programs.
Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings' |
  Where-Object PSChildName -match '(?i)NovaLauncher|NovaVoice|LagFreeDisplay' | ForEach-Object { Remove $_.PSPath "notification settings $($_.PSChildName)" }

if ($found -eq 0) { Write-Host '   nothing left over' }
exit 0
