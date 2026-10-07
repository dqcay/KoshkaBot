$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
}
& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$taskTestsDirectory = if (Test-Path -LiteralPath 'tests') { 'tests' } else { '.' }
& .\.venv\Scripts\python.exe -m unittest discover -s $taskTestsDirectory -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --collect-submodules ijson --name KoshkaBot app.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
& .\.venv\Scripts\python.exe -c "from pathlib import Path; from import_help import IMPORT_INSTRUCTIONS; Path('dist/IMPORT_INSTRUCTIONS.txt').write_text(IMPORT_INSTRUCTIONS, encoding='utf-8')"
if ($LASTEXITCODE -ne 0) { throw 'Could not create import instructions.' }
Write-Host 'Ready: dist\KoshkaBot.exe'
