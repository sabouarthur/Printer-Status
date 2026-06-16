<#
.SYNOPSIS
    Met a jour KodakMonitor.exe sur un poste magasin et active l'ecran de
    veille (verrouillage PIN) avec un PIN et un delai communs a la flotte.

.DESCRIPTION
    A executer (via l'outil de prise en main a distance) sur chacun des
    22 postes magasin. Le script :
      1. Utilise le dossier d'installation fixe (C:\PrinterStatus par defaut,
         identique sur tous les postes magasin).
      2. Arrete l'application si elle tourne, sauvegarde l'ancien exe.
      3. Telecharge la derniere version depuis GitHub (repo public) et les
         DLL SDK manquantes.
      4. Met a jour kodak_monitor_config.json : active l'ecran de veille,
         fixe le delai d'inactivite et le PIN de deverrouillage (hash
         PBKDF2-SHA256, compatible nativement avec kodak_monitor.py).
         Les autres reglages du magasin (hotfolder, compteur, etc.) ne
         sont pas touches.
      5. Relance KodakMonitor.exe.

.PARAMETER InstallDir
    Dossier d'installation de Kodak Monitor sur le poste magasin.

.PARAMETER Pin
    PIN de deverrouillage de l'ecran de veille, commun a la flotte.

.PARAMETER TimeoutMinutes
    Delai d'inactivite (minutes) avant verrouillage automatique.

.PARAMETER RepoRawBase
    Base URL "raw" du depot GitHub public contenant la derniere version.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File update_kodak_monitor.ps1
#>

param(
    [string]$InstallDir = "C:\PrinterStatus",
    [string]$Pin = "888",
    [int]$TimeoutMinutes = 10,
    [string]$RepoRawBase = "https://raw.githubusercontent.com/sabouarthur/Printer-Status/main"
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function New-KodakPinHash {
    <# Genere un hash strictement compatible avec _hash_config_pin() de kodak_monitor.py
       (PBKDF2-HMAC-SHA256, 200000 iterations, format "pbkdf2_sha256$iter$salt_hex$hash_hex"). #>
    param([string]$PlainPin, [int]$Iterations = 200000)
    $saltBytes = New-Object byte[] 16
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($saltBytes)
    $derive = New-Object System.Security.Cryptography.Rfc2898DeriveBytes(
        $PlainPin, $saltBytes, $Iterations, [System.Security.Cryptography.HashAlgorithmName]::SHA256
    )
    $hashBytes = $derive.GetBytes(32)
    $saltHex = -join ($saltBytes | ForEach-Object { $_.ToString("x2") })
    $hashHex = -join ($hashBytes | ForEach-Object { $_.ToString("x2") })
    return "pbkdf2_sha256" + '$' + $Iterations + '$' + $saltHex + '$' + $hashHex
}

function Set-JsonProp($Obj, $Name, $Value) {
    if ($Obj.PSObject.Properties.Name -contains $Name) {
        $Obj.$Name = $Value
    } else {
        $Obj | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
    }
}

Write-Step "Mise a jour Kodak Monitor - PIN ecran de veille + derniere version"

# --- 1. Dossier d'installation (fixe sur tous les postes magasin) ---
if (-not (Test-Path $InstallDir)) {
    Write-Error "Dossier introuvable : $InstallDir. Mise a jour annulee."
    exit 1
}
$exePath    = Join-Path $InstallDir "KodakMonitor.exe"
$configPath = Join-Path $InstallDir "kodak_monitor_config.json"
Write-Host "Dossier d'installation : $InstallDir"

# --- 2. Arreter l'application si elle tourne ---
$proc = Get-Process -Name "KodakMonitor" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($proc) {
    Write-Step "Arret de KodakMonitor.exe (PID $($proc.Id))"
    Stop-Process -Id $proc.Id -Force
    Start-Sleep -Seconds 2
} else {
    Write-Host "KodakMonitor.exe n'etait pas en cours d'execution."
}

# --- 3. Sauvegarder l'ancien exe ---
if (Test-Path $exePath) {
    $backupPath = Join-Path $installDir ("KodakMonitor.exe.bak_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
    Copy-Item -Path $exePath -Destination $backupPath -Force
    Write-Host "Ancienne version sauvegardee : $backupPath"
}

# --- 4. Telecharger la nouvelle version + DLL SDK manquantes ---
Write-Step "Telechargement de la derniere version depuis GitHub"
Invoke-WebRequest -Uri "$RepoRawBase/release/KodakMonitor.exe" -OutFile $exePath -UseBasicParsing

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
    $destFolder = Join-Path $installDir $f.Folder
    if (-not (Test-Path $destFolder)) { New-Item -ItemType Directory -Path $destFolder -Force | Out-Null }
    $dest = Join-Path $destFolder $f.Name
    if (-not (Test-Path $dest)) {
        Write-Host "Telechargement DLL manquante : $($f.Folder)\$($f.Name)"
        Invoke-WebRequest -Uri "$RepoRawBase/$($f.Folder)/$($f.Name)" -OutFile $dest -UseBasicParsing
    }
}

# --- 5. Mettre a jour la config (ecran de veille) sans toucher au reste ---
Write-Step "Mise a jour de la configuration (ecran de veille)"
if (Test-Path $configPath) {
    $config = Get-Content -Path $configPath -Raw | ConvertFrom-Json
} else {
    $config = [PSCustomObject]@{}
}

$pinHash = New-KodakPinHash -PlainPin $Pin

Set-JsonProp $config "screensaver_enabled" $true
Set-JsonProp $config "screensaver_timeout_minutes" $TimeoutMinutes
Set-JsonProp $config "screensaver_pin_hash" $pinHash

$config | ConvertTo-Json -Depth 5 | Set-Content -Path $configPath -Encoding utf8
Write-Host "Ecran de veille active - delai: $TimeoutMinutes min, PIN: $('*' * $Pin.Length)"

# --- 6. Relancer l'application ---
Write-Step "Relance de KodakMonitor.exe"
Start-Process -FilePath $exePath -WorkingDirectory $installDir
Start-Sleep -Seconds 2
if (Get-Process -Name "KodakMonitor" -ErrorAction SilentlyContinue) {
    Write-Host "OK - KodakMonitor.exe relance avec succes." -ForegroundColor Green
} else {
    Write-Warning "KodakMonitor.exe ne semble pas avoir redemarre - verification manuelle necessaire."
}
