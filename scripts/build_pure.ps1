# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    Pure desktop-pet onedir build (no AI, no optional feature modules).

.DESCRIPTION
    Builds the "pure" variant: desktop pet only (animation playback, click/drag/
    physics/multi-pet, self-talk bubbles, click sounds, tray+menus, settings,
    character switching). Everything else is cut at three layers:

      1. Behavior: packaging\pet_entry_pure.py sets DSH_PET_PURE=1 before any
         pet import; pet.feature_activation freezes the enabled-module set to
         empty (all 16 optional modules off: menus/services/hotkeys never
         appear). AI chat is additionally excluded from the bundle
         (--exclude-module pet.chat), same as the no-chat variant.
      2. Resources: no assets\chat, no resources\ (OCR models + presentmon),
         no integrations\. Animation webm (all 106 clips) IS bundled, plus the
         easter-egg images (assets\big_blue_fat_fish, ~16 MB - the egg entry
         ships as a pet-native feature).
      3. Dependencies: dedicated venv (.venv-pure) installed from
         requirements-pure.txt - no rapidocr/onnxruntime/cv2/numpy/winrt/
         edge-tts/psutil/keyring/pycaw/lunar-python/certifi anywhere near the
         module graph.

    Frameseq: by default the hot set (idle/move/turn/click/drag, 11 clips) is
    pre-converted at build time (same quality bar as the official release;
    runtime provision only converts ~1 clip per boot, so shipping without it
    leaves white-edge webm playback for weeks). Use -SkipHotFrameseq for the
    ultra-light build (~137 MB smaller; cold clips keep white edges longer).

    Examples:
      powershell -ExecutionPolicy Bypass -File scripts\build_pure.ps1
      powershell -ExecutionPolicy Bypass -File scripts\build_pure.ps1 -SkipHotFrameseq
      powershell -ExecutionPolicy Bypass -File scripts\build_pure.ps1 -PythonExe C:\Path\To\python.exe

    Installer: compile packaging\dsh-pet-pure.iss with Inno Setup 6 (ISCC.exe).
#>
param(
    [string]$PythonExe = '',
    [switch]$SkipVenv,
    [switch]$SkipBuild,
    [switch]$SkipSlim,
    [switch]$SkipSmoke,
    [switch]$SkipZip,
    [switch]$SkipHotFrameseq
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Encoding isolation (issue #26): force UTF-8 for the whole build.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$name = 'dsh-pet-pure'
$entry = 'packaging\pet_entry_pure.py'
$venvDir = Join-Path $root '.venv-pure'

# ---------------------------------------------------------------- toolchain
if ($PythonExe -eq '' -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $PythonExe = (Get-Command python).Source
}
if ($PythonExe -eq '') { throw 'python not found on PATH; pass -PythonExe <path>' }

$py = $PythonExe
if (-not $SkipVenv) {
    if (-not (Test-Path (Join-Path $venvDir 'Scripts\python.exe'))) {
        Write-Host "[0/5] Creating venv $venvDir ..." -ForegroundColor Cyan
        & $py -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed: $LASTEXITCODE" }
    }
    $py = Join-Path $venvDir 'Scripts\python.exe'
    Write-Host "[0/5] Installing pure-variant dependencies..." -ForegroundColor Cyan
    & $py -m pip install --upgrade pip --quiet
    & $py -m pip install -r requirements-pure.txt
    if ($LASTEXITCODE -ne 0) { throw "pip install failed: $LASTEXITCODE" }
}

# ---------------------------------------------------------------- hot frameseq
# Same call the CI uses minus the random/events include: convert ONLY the core
# closure 11 clips so the highest-playtime animations are white-edge-free from
# the first boot. Cold clips keep playing as webm (runtime provision upgrades
# them ~1 clip per boot).
if (-not $SkipBuild -and -not $SkipHotFrameseq) {
    Write-Host "[1/5] Preparing hot-set frameseq (idle/move/turn/click/drag, 11 clips)..." -ForegroundColor Cyan
    # Direct converter call: prepare_frameseq_bundle.py always ends with the
    # ALL-106-clips gate (its purpose is the full official bundle), while the
    # pure variant deliberately ships only the hot set. The converter without
    # --include converts exactly idle/move/turn/click/drag.
    & $py tools\convert_frameseq.py --character shenshen
    if ($LASTEXITCODE -ne 0) { throw "convert_frameseq failed: $LASTEXITCODE" }
}

# ---------------------------------------------------------------- PyInstaller
$dist = Join-Path $root 'dist-onedir'
$appDir = Join-Path $dist $name

if (-not $SkipBuild) {
    # Stop a running old exe so PyInstaller/slim are not blocked by live locks.
    $running = Get-Process -Name $name -ErrorAction SilentlyContinue
    if ($running) {
        Write-Host "[build] stopping running $name ..." -ForegroundColor Yellow
        $running | Stop-Process -Force
        Start-Sleep -Milliseconds 800
    }

    Write-Host "[2/5] Generating app icon..." -ForegroundColor Cyan
    & $py scripts\make_icon.py
    if ($LASTEXITCODE -ne 0) { throw "make_icon failed: $LASTEXITCODE" }

    # Variant marker: data dir becomes dsh-pet-standalone-pure (pet/config.py).
    $variantPy = Join-Path $root 'packaging\build_variant.py'
    [System.IO.File]::WriteAllText(
        $variantPy, "VARIANT = 'pure'`n",
        [System.Text.UTF8Encoding]::new($false)
    )

    $excludes = @('--exclude-module', 'pet.chat')
    # Heavy feature deps are absent from .venv-pure, but exclude anyway so a
    # polluted build machine cannot smuggle them into the module graph.
    foreach ($m in @('edge_tts', 'rapidocr', 'onnxruntime', 'cv2', 'numpy',
                     'omegaconf', 'keyring', 'win32ctypes', 'psutil', 'winrt',
                     'certifi', 'lunar_python', 'pytest', 'aiofiles',
                     'torch','transformers','datasets','langchain','langchain_core',
                     'langchain_openai','langsmith','langgraph','wandb','sentry_sdk',
                     'pandas','numba','llvmlite','pyarrow','polars','_polars_runtime_32',
                     'spacy','playwright','narwhals','sympy','fsspec',
                     'PyQt5','PyQt6','PySide2',
                     'matplotlib','matplotlib_inline','seaborn','IPython','ipykernel',
                     'jupyter_client','jupyter_core','nbformat','zmq',
                     'django','hypothesis', 'OpenGL')) {
        $excludes += @('--exclude-module', $m)
    }

    Write-Host "[3/5] PyInstaller --onedir building $name ..." -ForegroundColor Cyan
    & $py -m PyInstaller --noconfirm --clean --onedir --windowed --noupx `
        --name $name `
        --distpath $dist `
        --workpath (Join-Path $root 'build-pure') `
        --icon assets\icon.ico `
        --collect-all imageio_ffmpeg `
        --collect-all PySide6.QtMultimedia `
        --collect-all tzdata `
        --add-data 'assets\characters;assets\characters' `
        --add-data 'assets\big_blue_fat_fish;assets\big_blue_fat_fish' `
        --add-data 'pet\persona_presets;pet\persona_presets' `
        --add-data 'pet\menu_templates;pet\menu_templates' `
        --add-data 'assets\sounds;assets\sounds' `
        --add-data 'assets\icon.ico;assets' `
        --add-data 'LICENSE;.' `
        --add-data 'THIRD_PARTY_NOTICES.md;.' `
        @excludes `
        $entry
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
}

if (-not (Test-Path $appDir)) { throw "Build output missing: $appDir" }

# ---------------------------------------------------------------- slim + check
if (-not $SkipSlim) {
    Write-Host "[4/5] Slimming bundle (unused Qt modules / translations)..." -ForegroundColor Cyan
    & $py scripts\slim_bundle.py --app-dir $appDir
    if ($LASTEXITCODE -ne 0) { throw "Bundle slimming failed: $LASTEXITCODE" }
}

& $py scripts\check_bundle_encoding.py --dir $appDir
if ($LASTEXITCODE -ne 0) { throw "Bundle encoding check failed" }

# ---------------------------------------------------------------- smoke
if (-not $SkipSmoke) {
    & $py scripts\verify_bundle_qt.py --internal (Join-Path $appDir '_internal')
    if ($LASTEXITCODE -ne 0) { throw "bundle DLL chain verification failed" }

    $exePath = Join-Path $appDir "$name.exe"
    Write-Host "[5/5] Smoke launch $name.exe ..." -ForegroundColor Cyan
    $proc = Start-Process -FilePath $exePath -PassThru
    $t0 = Get-Date
    $ok = $false
    try {
        $deadline = $t0.AddSeconds(90)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            if ($proc.HasExited) { throw "exe exited early (code $($proc.ExitCode))" }
            $proc.Refresh()
            if ($proc.MainWindowHandle -ne 0) { $ok = $true; break }
        }
        if (-not $ok) { throw "no window appeared within 90s" }
        Write-Host ("[smoke] window appeared after {0:N1}s" -f ((Get-Date) - $t0).TotalSeconds) -ForegroundColor Green
    } finally {
        if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    }
}

# ---------------------------------------------------------------- report + zip
$sizeMB = [math]::Round((Get-ChildItem $appDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "onedir: $appDir ($sizeMB MB)" -ForegroundColor Green

if (-not $SkipZip) {
    $zip = Join-Path $dist "$name-portable.zip"
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path "$appDir\*" -DestinationPath $zip -CompressionLevel Optimal
    Write-Host ("portable zip: $zip ($([math]::Round((Get-Item $zip).Length/1MB,1)) MB)") -ForegroundColor Green
}
Write-Host 'Installer: compile packaging\dsh-pet-pure.iss with ISCC.exe (Inno Setup 6)' -ForegroundColor Green
