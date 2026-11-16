# One-time setup of the voice assistant on the N95 (run inside the copied TritonVoice folder):
#     powershell -ExecutionPolicy Bypass -File setup_n95.ps1
# Needs Python 3.13 from python.org (64-bit, "Add python.exe to PATH" ticked). Creates .venv here,
# installs requirements.txt, checks every model is in place, then starts the assistant (start.vbs).
# Add -Startup to also start it automatically when you log in.
param([switch]$Startup)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
# Windows' 260-character path limit (on by default on LTSC): onnxruntime's deepest file adds ~120 characters
# inside .venv, so a deep folder (Desktop, OneDrive...) makes pip fail halfway. C:\TritonVoice is fine.
if ($PSScriptRoot.Length -gt 100) {
    throw "This folder's path is too long for Windows ($($PSScriptRoot.Length) characters): move it to C:\TritonVoice and run again."
}

# 3.13 is what the laptop runs; 3.14 works too (every package has 3.14 wheels). Uses the first one found.
$python = "python"; $pyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in "3.13", "3.14") {
        & py "-$v" --version 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $python = "py"; $pyArgs = @("-$v"); break }
    }
}
& $python @pyArgs --version
if ($LASTEXITCODE -ne 0) { throw "Python 3.13 or 3.14 not found. Install it from python.org first." }

if (-not (Test-Path ".venv")) {
    Write-Host "Creating .venv ..."
    & $python @pyArgs -m venv .venv
}
Write-Host "Installing packages (a few minutes) ..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if (-not $?) { throw "pip install failed; see the messages above." }

# openWakeWord's feature models (melspectrogram, embedding) and built-in words aren't in its pip package:
# copy the bundled ones into it, or download them if the bundle has none
$owwDir = & .\.venv\Scripts\python.exe -c "import openwakeword, os; print(os.path.join(os.path.dirname(openwakeword.__file__), 'resources', 'models'))"
New-Item -ItemType Directory -Force $owwDir | Out-Null
if (Test-Path "openwakeword_models\melspectrogram.onnx") {
    Copy-Item "openwakeword_models\*.onnx" $owwDir -Force
} else {
    & .\.venv\Scripts\python.exe -c "from openwakeword.utils import download_models; download_models()"
}
if (-not (Test-Path (Join-Path $owwDir "melspectrogram.onnx"))) { throw "openWakeWord's models are missing from $owwDir" }

$settings = Get-Content settings.json -Raw | ConvertFrom-Json
$wake = $settings.wake_word
$voice = $settings.voice.name
$need = @("models\triton-mini.gguf", "models\$wake.onnx", "models\vosk-model-small-en-us-0.15",
          "models\stt\sherpa-onnx-whisper-base.en", "llama.cpp\llama-server.exe")
$missing = @($need | Where-Object { -not (Test-Path $_) })
# the voice is a Piper file or a sherpa-onnx voice folder (Kokoro, Kitten...)
if (-not ((Test-Path "models\piper\$voice.onnx") -or (Test-Path "models\tts\$voice"))) { $missing += "voice $voice" }
if ($missing) { throw "Missing: $($missing -join ', ')" }
& .\.venv\Scripts\python.exe -c "import pywinauto, sherpa_onnx, piper, openwakeword, vosk; print('packages OK')"

if ($Startup) {
    $lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Triton Voice.lnk"
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = "wscript.exe"
    $s.Arguments = "`"$PSScriptRoot\start.vbs`""
    $s.WorkingDirectory = $PSScriptRoot
    $s.Save()
    Write-Host "Starts automatically at login: $lnk"
}
# "Start Triton.bat" skips this whole setup once the marker exists
"set up $(Get-Date -Format s) with $(& .\.venv\Scripts\python.exe --version)" | Out-File -Encoding utf8 ".venv\triton_ready.txt"
Write-Host "Starting the assistant (tray icon; log in assistant.log). Say: $($wake -replace '_', ' ')"
Start-Process wscript.exe -ArgumentList "`"$PSScriptRoot\start.vbs`""
