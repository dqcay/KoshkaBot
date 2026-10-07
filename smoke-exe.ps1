# Starts only this build, checks its own window, and closes only the launched process.
$ErrorActionPreference = 'Stop'
$taskExe = Join-Path $PSScriptRoot 'dist\KoshkaBot.exe'
$taskProcess = Start-Process -FilePath $taskExe -WindowStyle Hidden -PassThru
$taskChild = $null
try {
    $taskDeadline = (Get-Date).AddSeconds(30)
    do {
        $taskChildInfo = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($taskProcess.Id)" |
            Where-Object { $_.ExecutablePath -eq $taskExe } | Select-Object -First 1
        if ($taskChildInfo) {
            $taskChild = Get-Process -Id $taskChildInfo.ProcessId -ErrorAction SilentlyContinue
            if ($taskChild -and $taskChild.MainWindowTitle -eq 'Кошка бот') { break }
        }
        if ($taskProcess.HasExited) { throw 'Executable exited before opening its window.' }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $taskDeadline)
    if (-not $taskChild -or $taskChild.MainWindowTitle -ne 'Кошка бот') { throw 'Window was not found.' }
    Write-Host 'EXE opened its main window successfully.'
    $null = $taskChild.CloseMainWindow()
    if (-not $taskProcess.WaitForExit(10000)) { throw 'Executable failed to close normally.' }
    if ($taskProcess.ExitCode -ne 0) { throw "Executable exit code: $($taskProcess.ExitCode)" }
    Write-Host 'EXE closed normally, exit code 0.'
} finally {
    if ($taskChild -and -not $taskChild.HasExited) { Stop-Process -Id $taskChild.Id -ErrorAction SilentlyContinue }
    if (-not $taskProcess.HasExited) { Stop-Process -Id $taskProcess.Id -ErrorAction SilentlyContinue }
}
