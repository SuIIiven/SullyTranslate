# SullyTranslate installer for Windows. Double-click install.bat (or: powershell -ExecutionPolicy Bypass -File install.ps1)
# Installs everything into one folder (default %LOCALAPPDATA%\SullyTranslate): a private Python, the speech
# engine, the models (~4-7 GB) and the OBS script, then adds the script to OBS. No admin rights needed.
param([string]$Dest = "")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the progress bar

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Run {
    & $args[0] $args[1..($args.Count - 1)]
    if ($LASTEXITCODE -ne 0) { throw "failed (exit $LASTEXITCODE): $($args -join ' ')" }
}

try {
    $Src = Split-Path -Parent $MyInvocation.MyCommand.Path
    if (-not $Dest) {
        $Dest = Join-Path $env:LOCALAPPDATA "SullyTranslate"
        # OBS's Lua scripting can't open paths with non-ASCII characters (e.g. an accented user name)
        if ($Dest -match '[^\x00-\x7F]') { $Dest = "C:\SullyTranslate" }
    }
    if ($Dest -match '[^\x00-\x7F]') { throw "Install folder must be plain ASCII (OBS limitation): $Dest" }
    Step "Installing SullyTranslate into $Dest"
    New-Item -ItemType Directory -Force -Path $Dest, "$Dest\state", "$Dest\tools" | Out-Null
    if ((Resolve-Path $Src).Path -ne (Resolve-Path $Dest).Path) {
        foreach ($f in "sullytranslate.py", "SullyTranslate.lua", "setup_obs.py", "requirements.txt",
                       "requirements-gpu.txt", "README.md", "LICENSE", "install.ps1", "install.bat") {
            Copy-Item (Join-Path $Src $f) $Dest -Force
        }
    }

    Step "Getting uv (private Python manager)"
    $uv = Join-Path $Dest "tools\uv.exe"
    if (-not (Test-Path $uv)) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $zip = Join-Path $env:TEMP "sullytranslate-uv.zip"
        Invoke-WebRequest "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" `
            -OutFile $zip -UseBasicParsing
        $tmp = Join-Path $env:TEMP "sullytranslate-uv"
        Expand-Archive $zip -DestinationPath $tmp -Force
        Copy-Item (Get-ChildItem $tmp -Recurse -Filter uv.exe | Select-Object -First 1).FullName $uv
        Remove-Item $zip, $tmp -Recurse -Force
    }
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Dest "tools\python"    # keep Python inside the install folder

    Step "Creating Python environment"
    Run $uv venv --python 3.12 --clear "$Dest\.venv"
    $py = "$Dest\.venv\Scripts\python.exe"
    Run $uv pip install --python $py -r "$Dest\requirements.txt"
    $gpu = $false
    try { & nvidia-smi -L *> $null; $gpu = ($LASTEXITCODE -eq 0) } catch { }
    if ($gpu) {
        Step "NVIDIA GPU found: installing CUDA libraries"
        Run $uv pip install --python $py -r "$Dest\requirements-gpu.txt"
    } else {
        Write-Host "No NVIDIA GPU found: SullyTranslate will run on the CPU (slower, uses smaller models)."
    }

    Step "Downloading speech + translation models (one time, several GB)"
    Run $py "$Dest\sullytranslate.py" --write-meta "$Dest\state\meta.json"
    Run $py "$Dest\sullytranslate.py" --download-models

    Step "Adding SullyTranslate to OBS"
    while ($true) {
        & $py "$Dest\setup_obs.py" "$Dest\SullyTranslate.lua"
        if ($LASTEXITCODE -eq 2) { Read-Host "Close OBS, then press Enter to continue"; continue }
        break
    }

    Write-Host ""
    Write-Host "Done! Start OBS: the captions appear as 'SullyTranslate: translation' in your scene." -ForegroundColor Green
    Write-Host "Settings: OBS -> Tools -> Scripts -> SullyTranslate.lua"
} catch {
    Write-Host ""
    Write-Host "Installation failed: $_" -ForegroundColor Red
    exit 1
}
