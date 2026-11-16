# One-time setup of the LAPTOP side of Triton (the N95 / TV side is the Triton Launcher bundle):
#   1. Triton's big brain (brain_server.py) starts hidden at every sign-in (Startup shortcut -> start_brain.vbs),
#      and its HTTPS certificate is made (big_brain.json gets the fingerprint the N95 pins).
#   2. Windows Firewall lets the N95 reach it: TCP 8765, this laptop's local subnet only, any Wi-Fi profile
#      (the Wi-Fi stays on Public). Needs admin once; Windows asks.
#   3. Triton Screen Share: starts with Windows, "Start as: share automatically", so "show / extend my laptop
#      here" said at the TV works without touching the laptop (paired PCs only; pairing is once, by clicking).
# Run:  powershell -ExecutionPolicy Bypass -File setup_laptop.ps1      (-NoFirewall to skip step 2)
param([switch]$NoFirewall,
      [string]$ScreenShare = (Join-Path (Split-Path $PSScriptRoot) "Triton Screen Share\dist\TritonScreenShare\TritonScreenShare.exe"))
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# 1. the big brain at sign-in, and its certificate + config now
& $python -m pip install --quiet cryptography
& $python -c "import brain_server as b; c = b.load_config(); print('Big brain certificate', c['cert_sha256'][:16] + '...', 'port', c['port'])"
if ($LASTEXITCODE -ne 0) { throw "Couldn't set up the big brain's certificate" }
$port = (Get-Content big_brain.json -Raw | ConvertFrom-Json).port
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Triton Big Brain.lnk"
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = "wscript.exe"
$s.Arguments = "`"$PSScriptRoot\start_brain.vbs`""
$s.WorkingDirectory = $PSScriptRoot
$s.Save()
Write-Host "Big brain starts at sign-in: $lnk"

# 2. firewall (admin)
if (-not $NoFirewall) {
    $rule = "Triton Big Brain (TCP $port, local subnet)"
    $cmd = "Remove-NetFirewallRule -DisplayName '$rule' -ErrorAction SilentlyContinue; " +
           "New-NetFirewallRule -DisplayName '$rule' -Direction Inbound -Protocol TCP -LocalPort $port " +
           "-RemoteAddress LocalSubnet -Profile Any -Action Allow | Out-Null"
    $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
         -ArgumentList "-NoProfile", "-Command", $cmd
    if ($p.ExitCode -eq 0) { Write-Host "Firewall: allowed TCP $port from the local network" }
    else { Write-Warning "Firewall rule not added (code $($p.ExitCode)); the N95 may not reach the big brain" }
}

# 3. Triton Screen Share: share automatically, start with Windows
$key = "HKCU:\Software\TritonScreenShare"
New-Item -Path $key -Force -ErrorAction SilentlyContinue | Out-Null
Set-ItemProperty -Path $key -Name StartAs -Value 1 -Type DWord
if (Test-Path $ScreenShare) {
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name TritonScreenShare `
                     -Value "`"$ScreenShare`" --background"
    Write-Host "Triton Screen Share starts with Windows and shares automatically to paired PCs"
} else {
    Write-Warning "Triton Screen Share not found at $ScreenShare; set 'Start with Windows' in its panel"
}

# Start the big brain now (unless it's already running: it refuses a second copy by itself)
Start-Process wscript.exe -ArgumentList "`"$PSScriptRoot\start_brain.vbs`""
Write-Host "Big brain starting in the background (log: brain_server.log). It takes about a minute to load."
