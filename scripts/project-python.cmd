@echo off
setlocal

set "SCRIBER_REPO_ROOT=%~dp0.."
set "SCRIBER_PROJECT_PYTHON="

if exist "%SCRIBER_REPO_ROOT%\venv\Scripts\python.exe" (
  set "SCRIBER_PROJECT_PYTHON=%SCRIBER_REPO_ROOT%\venv\Scripts\python.exe"
) else if exist "%SCRIBER_REPO_ROOT%\.venv\Scripts\python.exe" (
  set "SCRIBER_PROJECT_PYTHON=%SCRIBER_REPO_ROOT%\.venv\Scripts\python.exe"
)

if not defined SCRIBER_PROJECT_PYTHON (
  echo Scriber project Python was not found. Create it with: py -3.13 -m venv venv 1>&2
  exit /b 3
)

"%SCRIBER_PROJECT_PYTHON%" "%SCRIBER_REPO_ROOT%\scripts\verify_project_python.py"
if errorlevel 1 exit /b %ERRORLEVEL%

"%SCRIBER_PROJECT_PYTHON%" %*
exit /b %ERRORLEVEL%
