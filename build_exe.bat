@echo off
setlocal enabledelayedexpansion
echo ================================================
echo   COMPILATION KODAK MONITOR EN EXE
echo ================================================
echo.

REM Chercher Python 32-bit
set "PYTHON="
for %%v in (312-32 313-32 311-32 310-32) do (
    if exist "%LocalAppData%\Programs\Python\Python%%v\python.exe" (
        set "PYTHON=%LocalAppData%\Programs\Python\Python%%v\python.exe"
        goto found
    )
)
for %%v in (312-32 313-32 311-32 310-32) do (
    if exist "C:\Python%%v\python.exe" (
        set "PYTHON=C:\Python%%v\python.exe"
        goto found
    )
)

echo ERREUR: Python 32-bit introuvable
pause
exit /b 1

:found
echo Python: %PYTHON%
echo.

echo [1/3] Installation PyInstaller...
"%PYTHON%" -m pip install pyinstaller
echo.

echo [2/3] Installation dependances...
"%PYTHON%" -m pip install Pillow pywin32
echo.

echo [3/3] Compilation (1-2 min)...
cd /d "%~dp0"

echo Generation version Windows...
"%PYTHON%" "%~dp0make_version_info.py"
if errorlevel 1 (
    echo ERREUR: generation version impossible
    pause
    exit /b 1
)
echo.

REM Supprimer ancien build si present
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist KodakMonitor.spec del KodakMonitor.spec

"%PYTHON%" -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "KodakMonitor" ^
    --icon "icone-imprimante.ico" ^
    --version-file "version_info.txt" ^
    --add-data "icone-imprimante.ico;." ^
    --add-data "icone-imprimante.png;." ^
    --exclude-module KA6900 ^
    --exclude-module KA6900IP ^
    --exclude-module KA6900ColorMatch ^
    --exclude-module KA6900UsbCtrl ^
    --exclude-module chcusb ^
    --exclude-module SDKColorMatch ^
    --exclude-module SDKUsbCtrl ^
    kodak_monitor.py

echo.
echo ================================================
if exist "%~dp0dist\KodakMonitor.exe" (
    echo   SUCCES !
    echo.
    REM La config locale contient souvent des chemins machine et un PIN.
    REM Pour l'embarquer volontairement: build_exe.bat copy-config
    if /I "%~1"=="copy-config" (
        if exist "%~dp0kodak_monitor_config.json" (
            copy /Y "%~dp0kodak_monitor_config.json" "%~dp0dist\kodak_monitor_config.json" >nul
            echo   Config copiee dans dist\
        ) else (
            echo   Config source introuvable
        )
    ) else (
        echo   Config locale non copiee ^(utilisez copy-config si necessaire^)
    )
    REM Copier les DLLs SDK si presentes
    if exist "%~dp068xx" xcopy /E /I /Q /Y "%~dp068xx" "%~dp0dist\68xx\" >nul
    if exist "%~dp06900" xcopy /E /I /Q /Y "%~dp06900" "%~dp0dist\6900\" >nul
    echo.
    echo   L'exe est dans : %~dp0dist\KodakMonitor.exe
    echo.
    echo   Pour deployer, copiez TOUT dans un meme dossier :
    echo     KodakMonitor.exe
    echo     68xx\   (DLLs 6800/6850)
    echo     6900\   (DLLs 6900/6950)
    echo.
    echo   L'exe cherche les DLLs dans 68xx\ et 6900\
    echo   a cote de lui, PAS embarquees dedans.
) else (
    echo   ERREUR : voir les messages ci-dessus
)
echo ================================================
pause
