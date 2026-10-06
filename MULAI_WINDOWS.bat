@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto usepython
for %%V in (3.14 3.13 3.12 3.11) do (
  py -%%V -c "import sys" >nul 2>nul
  if not errorlevel 1 (
    py -%%V bootstrap.py
    goto done
  )
)
goto usepython
:usepython
where python >nul 2>nul
if errorlevel 1 goto missing
python bootstrap.py
goto done
:missing
echo Python belum ditemukan. Instal Python 3.11-3.14 64-bit dari python.org.
pause
exit /b 1
:done
set translator_exit_code=%errorlevel%
if not "%translator_exit_code%"=="0" pause
endlocal & exit /b %translator_exit_code%
