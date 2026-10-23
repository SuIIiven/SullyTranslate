# SullyTranslate uninstaller for Windows. Double-click uninstall.bat in the install folder.
# Removes the OBS script and both caption sources from every OBS scene collection, the script copy in OBS's
# own scripts folder, and every file the installer created (Python, libraries, models, caches).
$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }

try {
    $Dest = Split-Path -Parent $MyInvocation.MyCommand.Path
    if (-not (Test-Path (Join-Path $Dest "sullytranslate.py"))) {
        throw "This doesn't look like a SullyTranslate folder: $Dest"
    }
    $answer = Read-Host "Remove SullyTranslate ($Dest) and its sources from OBS? [y/N]"
    if ($answer -notmatch '^[yY]') { Write-Host "Nothing changed."; Read-Host "Press Enter to close"; exit 0 }
    Set-Location $env:TEMP                       # the folder can't be deleted while it is the current directory

    Step "Removing SullyTranslate from OBS"
    $py = Join-Path $Dest ".venv\Scripts\python.exe"
    if (Test-Path $py) {
        while ($true) {
            & $py (Join-Path $Dest "setup_obs.py") --remove
            if ($LASTEXITCODE -eq 2) { Read-Host "Close OBS, then press Enter to continue"; continue }
            break
        }
        if ($LASTEXITCODE -ne 0) { throw "could not update OBS's scene collections (exit $LASTEXITCODE); nothing deleted" }
    } else {
        Write-Host "Python environment missing: remove the script in OBS by hand (Tools > Scripts > SullyTranslate.lua > -)."
    }

    Step "Stopping the speech engine"
    $engines = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Dest, [StringComparison]::OrdinalIgnoreCase) }
    if ($engines) { $engines | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Start-Sleep 1 } else { Write-Host "Not running." }

    # A copy of the script in OBS's own scripts folder (needs administrator rights there).
    foreach ($obs in @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }) {
        $f = Join-Path $obs "obs-studio\data\obs-plugins\frontend-tools\scripts\SullyTranslate.lua"
        if (Test-Path $f) {
            Step "Removing $f"
            try { Remove-Item -LiteralPath $f -Force }
            catch { Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile -Command Remove-Item -LiteralPath '$f' -Force" }
        }
    }

    Step "Deleting installed files"
    if (Test-Path (Join-Path $Dest ".git")) {
        # A development checkout: keep the source code, delete only what the installer generated.
        foreach ($sub in ".venv", "models", "tools", "state", "__pycache__") {
            $p = Join-Path $Dest $sub
            if (Test-Path $p) { Remove-Item -LiteralPath $p -Recurse -Force }
        }
        Write-Host "Kept the source code in $Dest (it is a git checkout); removed the environment, models and state."
    } else {
        Remove-Item -LiteralPath $Dest -Recurse -Force
        Write-Host "Deleted $Dest."
    }

    Write-Host ""
    Write-Host "SullyTranslate has been removed." -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "Uninstall failed: $_" -ForegroundColor Red
}
Read-Host "Press Enter to close"
