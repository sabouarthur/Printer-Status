@echo off
setlocal
echo ============================================
echo   COMPILATION KODAK RAPPORT EN EXE
echo ============================================
echo.

REM --- Chemin Python 32-bit ---
set "PYTHON=C:\Users\arthu_gsbft5u\AppData\Local\Programs\Python\Python312-32\python.exe"
set "SCRIPT_DIR=%~dp0"
set "ICON_FILE=%SCRIPT_DIR%kodak_rapport.ico"

REM --- Installer PyInstaller si necessaire ---
echo Installation/verification de PyInstaller...
"%PYTHON%" -m pip install pyinstaller --quiet
echo Installation/verification de openpyxl...
"%PYTHON%" -m pip install openpyxl --quiet
echo.

REM --- Compiler ---
echo Compilation en cours...
if exist "%SCRIPT_DIR%dist\Kodak_Rapport.exe" del /f /q "%SCRIPT_DIR%dist\Kodak_Rapport.exe" >nul 2>nul
if exist "%ICON_FILE%" (
    echo Icone detectee: %ICON_FILE%
    "%PYTHON%" -m PyInstaller --noconfirm --onefile --windowed --name "Kodak_Rapport" --icon "%ICON_FILE%" --add-data "%ICON_FILE%;." --add-data "%SCRIPT_DIR%icone-imprimante.png;." "%SCRIPT_DIR%kodak_rapport.py"
) else (
    echo Aucune icone .ico detectee, compilation sans icone personnalisee.
    "%PYTHON%" -m PyInstaller --noconfirm --onefile --windowed --name "Kodak_Rapport" "%SCRIPT_DIR%kodak_rapport.py"
)
set "BUILD_RC=%ERRORLEVEL%"

echo.
if not "%BUILD_RC%"=="0" (
    echo ERREUR : PyInstaller a echoue ^(code %BUILD_RC% ^)
) else (
    if exist "%SCRIPT_DIR%dist\Kodak_Rapport.exe" (
        echo ============================================
        echo   OK : dist\Kodak_Rapport.exe
        echo ============================================
        echo.
        echo Copie dans le dossier courant...
        copy "%SCRIPT_DIR%dist\Kodak_Rapport.exe" "%SCRIPT_DIR%Kodak_Rapport.exe" >nul
        echo Fichier : %SCRIPT_DIR%Kodak_Rapport.exe
    ) else (
        echo ERREUR : la compilation a echoue ^(exe introuvable^)
    )
)

echo.
pause
endlocal
