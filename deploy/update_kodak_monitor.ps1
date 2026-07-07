<#
.SYNOPSIS
    Updates KodakMonitor.exe on a store PC to the latest published version.

.DESCRIPTION
    Run this (via the remote support tool) on each of the 22 store PCs.
    The script:
      1. Uses the fixed install folder (C:\PrinterStatus by default,
         identical on every store PC).
      2. Compares the local exe version to the latest published version
         (release/VERSION.txt, very lightweight): if it's already the
         latest, the exe download step is skipped.
      3. Stops the application if it's running, backs up the old exe.
      4. Downloads the latest version from GitHub (public repo, unless
         already up to date) and any missing SDK DLLs.
      5. Restarts KodakMonitor.exe.
      6. Checks that a shortcut exists in the current user's Startup
         folder (auto-start on every Windows session); creates or fixes
         it if needed.

.PARAMETER InstallDir
    Kodak Monitor install folder on the store PC.

.PARAMETER RepoRawBase
    "Raw" base URL of the public GitHub repo containing the latest version.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File update_kodak_monitor.ps1
#>

param(
    [string]$InstallDir = "C:\PrinterStatus",
    [string]$RepoRawBase = "https://raw.githubusercontent.com/sabouarthur/Printer-Status/main"
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

Write-Step "Updating Kodak Monitor - latest version"

# --- 1. Install folder (fixed on every store PC) ---
if (-not (Test-Path $InstallDir)) {
    Write-Error "Folder not found: $InstallDir. Update cancelled."
    exit 1
}
$exePath = Join-Path $InstallDir "KodakMonitor.exe"
Write-Host "Install folder: $InstallDir"

# --- 2. Check whether the local exe is already the latest version ---
$needsDownload = $true
if (Test-Path $exePath) {
    try {
        $localVersion = (Get-Item $exePath).VersionInfo.FileVersion
        $remoteVersion = (Invoke-WebRequest -Uri "$RepoRawBase/release/VERSION.txt" -UseBasicParsing).Content.Trim()
        Write-Host "Local version: $localVersion / Remote version: $remoteVersion"
        if ($localVersion -and $remoteVersion -and ($localVersion -eq $remoteVersion)) {
            $needsDownload = $false
            Write-Host "Already up to date - skipping exe download."
        }
    } catch {
        Write-Host "Could not check the remote version ($($_.Exception.Message)) - downloading as a precaution."
    }
} else {
    Write-Host "No local exe found - download required."
}

# --- 3. Stop the application if it's running (PyInstaller --onefile often
#         launches 2 processes: the bootloader + the app itself -> stop
#         ALL processes found, not just the first one) ---
$procs = Get-Process -Name "KodakMonitor" -ErrorAction SilentlyContinue
if ($procs) {
    Write-Step "Stopping KodakMonitor.exe ($($procs.Count) process(es) found: $($procs.Id -join ', '))"
    $procs | Stop-Process -Force
    Start-Sleep -Seconds 2
    # Check: if a file is still locked, wait a bit longer
    $retries = 0
    while ((Get-Process -Name "KodakMonitor" -ErrorAction SilentlyContinue) -and $retries -lt 5) {
        Start-Sleep -Seconds 1
        $retries++
    }
} else {
    Write-Host "KodakMonitor.exe was not running."
}

# --- 4. Back up the old exe ---
if (Test-Path $exePath) {
    $backupPath = Join-Path $InstallDir ("KodakMonitor.exe.bak_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
    Copy-Item -Path $exePath -Destination $backupPath -Force
    Write-Host "Previous version backed up: $backupPath"
}

# --- 5. Download the new version (if needed) + missing SDK DLLs ---
if ($needsDownload) {
    Write-Step "Downloading the latest version from GitHub"
    $downloadOk = $false
    for ($i = 1; $i -le 3; $i++) {
        try {
            Invoke-WebRequest -Uri "$RepoRawBase/release/KodakMonitor.exe" -OutFile $exePath -UseBasicParsing
            $downloadOk = $true
            break
        } catch {
            Write-Host "Attempt $i failed ($($_.Exception.Message)), retrying in 2s..."
            Start-Sleep -Seconds 2
        }
    }
    if (-not $downloadOk) {
        Write-Error "Could not download/replace KodakMonitor.exe after 3 attempts. Update cancelled."
        exit 1
    }
} else {
    Write-Step "Download step skipped (exe already up to date)"
}

$dllFiles = @(
    @{Folder = "68xx"; Name = "chcusb.dll"},
    @{Folder = "68xx"; Name = "SDKColorMatch.dll"},
    @{Folder = "68xx"; Name = "SDKUsbCtrl.dll"},
    @{Folder = "6900"; Name = "KA6900.dll"},
    @{Folder = "6900"; Name = "KA6900ColorMatch.dll"},
    @{Folder = "6900"; Name = "KA6900IP.dll"},
    @{Folder = "6900"; Name = "KA6900UsbCtrl.dll"},
    @{Folder = "6900"; Name = "msvcp100.dll"},
    @{Folder = "6900"; Name = "msvcr100.dll"}
)
foreach ($f in $dllFiles) {
    $destFolder = Join-Path $InstallDir $f.Folder
    if (-not (Test-Path $destFolder)) { New-Item -ItemType Directory -Path $destFolder -Force | Out-Null }
    $dest = Join-Path $destFolder $f.Name
    if (-not (Test-Path $dest)) {
        Write-Host "Downloading missing DLL: $($f.Folder)\$($f.Name)"
        Invoke-WebRequest -Uri "$RepoRawBase/$($f.Folder)/$($f.Name)" -OutFile $dest -UseBasicParsing
    }
}

# --- 6. Restart the application ---
Write-Step "Restarting KodakMonitor.exe"
Start-Process -FilePath $exePath -WorkingDirectory $InstallDir
Start-Sleep -Seconds 2
if (Get-Process -Name "KodakMonitor" -ErrorAction SilentlyContinue) {
    Write-Host "OK - KodakMonitor.exe restarted successfully." -ForegroundColor Green
} else {
    Write-Warning "KodakMonitor.exe does not seem to have restarted - manual check required."
}

# --- 7. Check/create the auto-start shortcut ---
Write-Step "Checking the auto-start shortcut"
$startupFolder = [System.Environment]::GetFolderPath("Startup")
$shortcutPath  = Join-Path $startupFolder "KodakMonitor.lnk"

if (Test-Path $shortcutPath) {
    # Check that the shortcut points to the right exe
    $wsh = New-Object -ComObject WScript.Shell
    $existing = $wsh.CreateShortcut($shortcutPath)
    if ($existing.TargetPath -ieq $exePath) {
        Write-Host "Startup shortcut already correct: $shortcutPath"
    } else {
        Write-Host "Existing shortcut is incorrect ($($existing.TargetPath)) - updating..."
        $sc = $wsh.CreateShortcut($shortcutPath)
        $sc.TargetPath       = $exePath
        $sc.WorkingDirectory = $InstallDir
        $sc.Description      = "Kodak Printer Monitor"
        $sc.Save()
        Write-Host "Shortcut updated: $shortcutPath" -ForegroundColor Green
    }
} else {
    $wsh = New-Object -ComObject WScript.Shell
    $sc  = $wsh.CreateShortcut($shortcutPath)
    $sc.TargetPath       = $exePath
    $sc.WorkingDirectory = $InstallDir
    $sc.Description      = "Kodak Printer Monitor"
    $sc.Save()
    Write-Host "Shortcut created: $shortcutPath" -ForegroundColor Green
}
Write-Host "The application will start automatically on every Windows session."
