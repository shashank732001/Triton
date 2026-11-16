# Packages the "Hey Triton" voice assistant (..\Voice Assistant) into voice-build\TritonVoice\TritonVoice.exe,
# a standalone folder that needs no Python on the NUC. build.ps1 copies it into the bundle as voice\.
#   1. make_n95_bundle.py gathers what the N95 runs (into voice-build\stage; only changed files are copied):
#      the brain (fine-tuned Qwen3-0.6B Q4_K_M as models\triton-mini.gguf), llama.cpp's llama-server, the wake
#      word + voice check, Vosk, Whisper (ears), the Piper voice, and the N95's settings.json (wake word, the
#      laptop big brain's HTTPS address + key + pinned certificate, screen-share voice control).
#   2. PyInstaller freezes assistant.py with its packages.
#   3. The models, llama.cpp and settings go next to TritonVoice.exe (the assistant looks there first).
param([string]$Brain = "")
# "Continue": pip/PyInstaller log to stderr, which Windows PowerShell would otherwise treat as fatal; exit codes are checked
$ErrorActionPreference = "Continue"
$root   = $PSScriptRoot
$src    = Join-Path (Split-Path $root) "Voice Assistant"
$python = Join-Path $src ".venv\Scripts\python.exe"
$work   = Join-Path $root "voice-build"
$stage  = Join-Path $work "stage"
$out    = Join-Path $work "TritonVoice"

$bundleArgs = @((Join-Path $src "make_n95_bundle.py"), "--out", $stage)
if ($Brain) { $bundleArgs += @("--brain", $Brain) }
Push-Location $src
& $python @bundleArgs
$rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { throw "make_n95_bundle.py failed" }

& $python -m pip install --quiet pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Couldn't install PyInstaller" }

# The winrt package ships an old C++ runtime (msvcp140.dll 14.29) that crashes onnxruntime when it loads
# first, which PyInstaller's import scan does. Preload Windows' newer copy in every Python process of the build.
$msvcp = Join-Path $env:WINDIR "System32\msvcp140.dll"
$preload = Join-Path $work "preload"
New-Item -ItemType Directory -Force $preload | Out-Null
"import ctypes; ctypes.WinDLL(r'$msvcp')" | Set-Content (Join-Path $preload "sitecustomize.py") -Encoding ascii
$env:PYTHONPATH = $preload

if (Test-Path $out) { Remove-Item $out -Recurse -Force }
& $python -m PyInstaller (Join-Path $src "assistant.py") --name TritonVoice --noconfirm --windowed `
    --distpath $work --workpath (Join-Path $work "tmp") --specpath $work `
    --paths $src `
    --collect-all openwakeword --collect-all vosk --collect-all winrt `
    --collect-all sherpa_onnx --collect-all moonshine_voice --collect-all livekit --collect-all soundcard --collect-all piper --collect-all pywinauto --collect-submodules comtypes `
    --hidden-import pystray._win32 --hidden-import test_console --hidden-import events `
    --hidden-import sklearn.linear_model._logistic --hidden-import sklearn.pipeline `
    --hidden-import sklearn.preprocessing._data --hidden-import sklearn.preprocessing._function_transformer `
    --exclude-module brain_server --exclude-module cryptography `
    --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$env:PYTHONPATH = $null

# Same problem at run time: give the packaged app the newer runtime everywhere (it's backward compatible)
Get-ChildItem $out -Recurse -Filter "msvcp140.dll" | ForEach-Object { Copy-Item $msvcp $_.FullName -Force }

# Everything make_n95_bundle.py gathered except the source code (it's inside the exe now)
foreach ($item in "models", "llama.cpp", "settings.json", "shortcuts.json", "BRAIN.txt") {
    Copy-Item (Join-Path $stage $item) $out -Recurse -Force
}
# llama-server needs the Visual C++ runtime; the N95 may not have it installed, so it goes next to it
# (Microsoft's "app-local" deployment)
foreach ($dll in "msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll") {
    $f = Join-Path $env:WINDIR "System32\$dll"
    if (Test-Path $f) { Copy-Item $f (Join-Path $out "llama.cpp") -Force }
}
# The test console (watch each step live, type commands): the exe opens it with --console
"@echo off`r`nrem Triton test console: watch each step live and type commands (Triton must be running)`r`nstart `"`" `"%~dp0TritonVoice.exe`" --console`r`n" |
    Set-Content (Join-Path $out "Test Console.bat") -Encoding ascii -NoNewline
$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum
"Voice assistant: $(Join-Path $out 'TritonVoice.exe') ({0:N0} MB)" -f ($size / 1MB)
