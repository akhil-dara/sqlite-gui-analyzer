<#
.SYNOPSIS
    Runs a built SQLite GUI Analyzer exe with --self-test and fails unless it passes.

.DESCRIPTION
    The built programs are windowed (no console), so the result is the exit code and the report
    is read back from the --self-test-log file.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\run_self_test.ps1 -Exe dist\SQLiteGUIAnalyzer\SQLiteGUIAnalyzer.exe
#>
param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [string]$Log = ""
)
$ErrorActionPreference = "Stop"

$Exe = (Resolve-Path -LiteralPath $Exe).Path
if (-not $Log) {
    $Log = Join-Path ([IO.Path]::GetTempPath()) ("sga-self-test-" + [guid]::NewGuid().ToString("N") + ".log")
}
if (Test-Path -LiteralPath $Log) { Remove-Item -LiteralPath $Log }

Write-Host "Self-test: $Exe"
$started = Get-Date
$process = Start-Process -FilePath $Exe -ArgumentList "--self-test", "--self-test-log", "`"$Log`"" -Wait -PassThru
$seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)

if (Test-Path -LiteralPath $Log) {
    Get-Content -LiteralPath $Log | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "  (no log was written)"
}
if ($process.ExitCode -ne 0) {
    throw "Self-test of $Exe failed with exit code $($process.ExitCode) after $seconds s"
}
Write-Host "Self-test passed in $seconds s (exit code 0)"
