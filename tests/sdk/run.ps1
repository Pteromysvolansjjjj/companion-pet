param([Parameter(Mandatory = $true)][string]$DshAppDirectory)
$ErrorActionPreference = 'Stop'
$dshExecutable = Join-Path $DshAppDirectory 'DeepSeek Harness.exe'
$testScript = Join-Path $PSScriptRoot 'integration.mjs'
if (-not (Test-Path -LiteralPath $dshExecutable -PathType Leaf)) {
  throw "DeepSeek Harness.exe was not found in $DshAppDirectory"
}
$previousRunAsNode = $env:ELECTRON_RUN_AS_NODE
try {
  $env:ELECTRON_RUN_AS_NODE = '1'
  # The pipeline makes PowerShell await this GUI-subsystem executable in Node mode.
  & $dshExecutable $testScript --app-dir $DshAppDirectory | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "SDK integration failed (exit $LASTEXITCODE)" }
} finally {
  if ($null -eq $previousRunAsNode) { Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue }
  else { $env:ELECTRON_RUN_AS_NODE = $previousRunAsNode }
}
