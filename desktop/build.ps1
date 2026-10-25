param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler = Join-Path $framework 'csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319'
    $compiler = Join-Path $framework 'csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework 4 C# compiler was not found.' }
$outputDirectory = Join-Path $ProjectRoot 'dist'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$output = Join-Path $outputDirectory 'LolCompanion-0.7.1.exe'
$references = @(
    (Join-Path $framework 'System.dll'),
    (Join-Path $framework 'System.Core.dll'),
    (Join-Path $framework 'System.Drawing.dll'),
    (Join-Path $framework 'System.Windows.Forms.dll'),
    (Join-Path $framework 'System.Web.Extensions.dll'),
    (Join-Path $framework 'WPF\WindowsBase.dll'),
    (Join-Path $framework 'WPF\PresentationCore.dll'),
    (Join-Path $framework 'WPF\PresentationFramework.dll'),
    (Join-Path $framework 'System.Xaml.dll')
)
$compilerArguments = @('/nologo', '/target:winexe', '/platform:anycpu', '/optimize+', '/codepage:65001', ('/out:' + $output))
foreach ($reference in $references) { $compilerArguments += ('/reference:' + $reference) }
$compilerArguments += (Join-Path $PSScriptRoot 'LolCompanion.cs')
$compilerArguments += (Join-Path $PSScriptRoot 'ChatInput.cs')
& $compiler @compilerArguments
if ($LASTEXITCODE -ne 0) { throw ('Desktop compilation failed with exit code ' + $LASTEXITCODE) }
Write-Output ('Built ' + $output)
