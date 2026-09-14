<#
.SYNOPSIS
  Build the Windows installer: dist\PrecisionTouchdownAnalyzer-Setup-<version>.exe

.DESCRIPTION
  Run from the repository root, inside the project's virtual environment
  (the one with the [ui,analysis] extras and pyinstaller installed):

      pip install -e ".[ui,analysis]" pyinstaller
      .\packaging\windows\build.ps1

  Steps: icon -> PyInstaller application folder -> zip -> PyInstaller setup
  program with the zip inside. To ship ffmpeg with the program, put
  ffmpeg.exe and ffprobe.exe into vendor\ffmpeg\ first (the gyan.dev
  "essentials" build works); otherwise the installed program looks for
  ffmpeg on PATH and the control window says so if it is missing.
#>
# "Continue": PyInstaller logs to stderr, which Windows PowerShell 5.1 would
# otherwise turn into a terminating error; exit codes are checked instead.
$ErrorActionPreference = "Continue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }

Write-Host "== icon"
& $python packaging\make_icon.py packaging\out

Write-Host "== application folder"
if (Test-Path build\dist) { Remove-Item build\dist -Recurse -Force }
& $python -m PyInstaller packaging\windows\app.spec --noconfirm --distpath build\dist --workpath build\work --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (app) failed" }

$app = "build\dist\PrecisionTouchdownAnalyzer"
Copy-Item packaging\out\icon.ico "$app\icon.ico" -Force
Copy-Item packaging\out\icon.png "$app\icon.png" -Force
Copy-Item LICENSE "$app\LICENSE.txt" -Force

Write-Host "== smoke test of the folder"
& "$app\touchdown-analyzer.exe" --version
if ($LASTEXITCODE -ne 0) { throw "the built CLI does not run" }

Write-Host "== app.zip"
if (Test-Path build\app.zip) { Remove-Item build\app.zip -Force }
# Python's zipfile rather than Compress-Archive, which now and then fails
# silently on a folder the virus scanner is still busy with.
& $python -c "import shutil; shutil.make_archive('build/app', 'zip', '$app')"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path build\app.zip)) { throw "app.zip was not written" }

Write-Host "== setup program"
New-Item -ItemType Directory -Force dist | Out-Null
& $python -m PyInstaller packaging\windows\setup.spec --noconfirm --distpath dist --workpath build\work-setup --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (setup) failed" }

Get-ChildItem dist\PrecisionTouchdownAnalyzer-Setup-*.exe | ForEach-Object {
  Write-Host ("== done: {0}  ({1:N1} MB)" -f $_.FullName, ($_.Length / 1MB))
}
