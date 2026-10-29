param(
    [ValidateSet('tiny', 'base', 'small', 'medium')]
    [string]$Model = 'small',
    [switch]$SkipModel
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $PSScriptRoot '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    Write-Output 'Creating virtual environment (vision/.venv)...'
    $base = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $base) { throw 'Python 3.11+ was not found on PATH.' }
    & $base -m venv $venv
}

Write-Output 'Installing Python dependencies...'
& $python -m pip install --disable-pip-version-check --upgrade pip | Out-Null
& $python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

if (-not $SkipModel) {
    $target = Join-Path $PSScriptRoot ('models\faster-whisper-' + $Model)
    if (Test-Path -LiteralPath (Join-Path $target 'model.bin')) {
        Write-Output ('Whisper model already present: ' + $target)
    } else {
        Write-Output ('Downloading faster-whisper-' + $Model + ' (about 500 MB for small)...')
        $repo = 'Systran/faster-whisper-' + $Model
        $script = @'
import sys
from huggingface_hub import snapshot_download
repo, target = sys.argv[1], sys.argv[2]
snapshot_download(repo, local_dir=target, allow_patterns=['*.bin', '*.json', '*.txt', '*.model'])
print('OK', target)
'@
        $temp = Join-Path $env:TEMP 'lolvision-model.py'
        Set-Content -LiteralPath $temp -Value $script -Encoding utf8
        & $python $temp $repo $target
        if ($LASTEXITCODE -ne 0) { throw 'Model download failed. Check the network, or copy the model directory manually.' }
    }
}

Write-Output ''
Write-Output 'Self-checks:'
& $python (Join-Path $PSScriptRoot 'selftest.py')
Write-Output ''
Write-Output 'Done. Next: npm run vision:probe  (or press F2 in game after starting the companion).'
