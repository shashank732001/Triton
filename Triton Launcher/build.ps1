# Builds the launcher and assembles a copy-anywhere folder + zip in .\dist
# The voice assistant is packaged once by build-voice.ps1; pass -RebuildVoice after changing ..\Voice Assistant.
param([switch]$RebuildVoice,
      [string]$Display = (Join-Path $PSScriptRoot "..\Triton Screen Share\dist\TritonScreenShare"))
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$out  = Join-Path $root "dist\TritonLauncher"
$bin  = Join-Path $root "src\bin\Release\net48"

dotnet build (Join-Path $root "src\TritonLauncher.csproj") -c Release -nologo -v q
if ($LASTEXITCODE -ne 0) { throw "Build failed" }

if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force $out | Out-Null

# Only what's needed at runtime (the WPF dll and runtimes\ folder are not used).
foreach ($f in "TritonLauncher.exe", "TritonLauncher.exe.config", "Microsoft.Web.WebView2.Core.dll",
               "Microsoft.Web.WebView2.WinForms.dll", "WebView2Loader.dll") {
    Copy-Item (Join-Path $bin $f) $out
}
Copy-Item (Join-Path $root "web") $out -Recurse
Copy-Item (Join-Path $root "wallpapers") $out -Recurse
foreach ($f in "config.json", "Install.cmd", "Uninstall.cmd", "remove-old.ps1", "README.txt") {
    Copy-Item (Join-Path $root $f) $out
}

$voice = Join-Path $root "voice-build\TritonVoice"
if ($RebuildVoice -or -not (Test-Path (Join-Path $voice "TritonVoice.exe"))) { & (Join-Path $root "build-voice.ps1") }
Copy-Item $voice (Join-Path $out "voice") -Recurse
Remove-Item (Join-Path $out "voice\assistant.log") -ErrorAction SilentlyContinue   # from test runs

# Triton Screen Share (screen sharing, mic, camera), built by ..\Triton Screen Share\build.bat.
# Install.cmd runs its install-all.ps1, which installs the extend driver from extend-driver\ with devcon,
# so only the driver files go in, not the 171 MB "VDD Control" app beside them.
$display = $Display
if (Test-Path (Join-Path $display "TritonScreenShare.exe")) {
    robocopy $display (Join-Path $out "display") /e /xf "VDD Control.exe" "VDD Control.pdb" /njh /njs /nfl /ndl /np | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying Triton Screen Share failed" }
    $global:LASTEXITCODE = 0   # robocopy's 1 = "files copied"
} else {
    Write-Warning "Triton Screen Share not built ($display); the bundle will not include screen sharing."
}

$zip = Join-Path $root "dist\TritonLauncher.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $out -DestinationPath $zip

$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum
"Bundle: $out ({0:N0} KB)" -f ($size / 1KB)
"Zip:    $zip"
