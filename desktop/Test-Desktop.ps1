param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$executable = Join-Path $ProjectRoot 'dist\LolCompanion-0.7.1.exe'
$testDirectory = Join-Path $PSScriptRoot 'test-output'
New-Item -ItemType Directory -Path $testDirectory -Force | Out-Null

function Invoke-Renderer([string]$Arguments, [string[]]$Lines, [int]$HoldOpenMs = 0) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $executable
    $start.Arguments = $Arguments
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $start.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    if (-not $process.Start()) { throw 'Renderer did not start.' }
    $outputTask = $process.StandardOutput.ReadToEndAsync()
    $errorTask = $process.StandardError.ReadToEndAsync()
    foreach ($line in $Lines) { $process.StandardInput.WriteLine($line) }
    if ($HoldOpenMs -gt 0) {
        $process.StandardInput.Flush()
        [System.Threading.Thread]::Sleep($HoldOpenMs)
    }
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(15000)) {
        $process.Kill()
        throw 'Renderer test timed out.'
    }
    $result = [pscustomobject]@{
        ExitCode = $process.ExitCode
        Output = $outputTask.Result
        Error = $errorTask.Result
    }
    $process.Dispose()
    return $result
}

$preview = Join-Path $testDirectory 'preview.png'
$render = Invoke-Renderer ('--render-test "' + $preview + '"') @()
$render.Output | Set-Content -LiteralPath (Join-Path $testDirectory 'render-result.jsonl') -Encoding UTF8
if ($render.ExitCode -ne 0 -or $render.Error) { throw ('Render failed: ' + $render.Error) }
$renderResult = $render.Output.Trim() | ConvertFrom-Json
if (-not $renderResult.ok -or -not (Test-Path -LiteralPath $preview)) { throw 'Render verification failed.' }
Add-Type -AssemblyName System.Drawing
foreach ($name in @('preview', 'preview-idle', 'preview-compact', 'preview-emoji', 'preview-settings', 'preview-chat')) {
    $alphaImage = [System.Drawing.Image]::FromFile((Join-Path $testDirectory ($name + '.png')))
    $blackImage = New-Object System.Drawing.Bitmap($alphaImage.Width, $alphaImage.Height, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
    $graphics = [System.Drawing.Graphics]::FromImage($blackImage)
    try {
        $graphics.Clear([System.Drawing.Color]::Black)
        $destinationRectangle = New-Object System.Drawing.Rectangle(0, 0, $alphaImage.Width, $alphaImage.Height)
        $graphics.DrawImage($alphaImage, $destinationRectangle, 0, 0, $alphaImage.Width, $alphaImage.Height, [System.Drawing.GraphicsUnit]::Pixel)
        $blackImage.Save((Join-Path $testDirectory ($name + '-black.png')), [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally { $graphics.Dispose(); $blackImage.Dispose(); $alphaImage.Dispose() }
}

$ordinary = 'x<silent/>y'
$replyJson = @{ type = 'reply'; text = $ordinary } | ConvertTo-Json -Compress
$twentyJoinedEmojiLines = '{"type":"reply","lines":["' + ('\ud83d\udc69\u200d\ud83d\udcbb' * 20) + '"]}'
$twentyJoinedEmojiText = '{"type":"reply","text":"' + ('\ud83d\udc69\u200d\ud83d\udcbb' * 20) + '"}'
$oversizedPage = @{ type = 'reply'; lines = @('x' * 4097) } | ConvertTo-Json -Compress
$protocol = Invoke-Renderer '--smoke-test' @(
    $replyJson,
    '{"type":"reply","lines":["\u7b2c\u4e00\u53e5","\u7b2c\u4e8c\u53e5","\u7b2c\u4e09\u53e5"],"holdMs":500}',
    '{"type":"status","text":"connected"}',
    '{"type":"reply","text":12}',
    '{"type":"reply","text":"123456789012345678901"}',
    '{"type":"reply","lines":[]}',
    '{"type":"reply","lines":["one\ntwo"]}',
    '{"type":"reply","lines":["\ud800"]}',
    '{"type":"reply","lines":["bad duration"],"holdMs":499}',
    '{"type":"reply","lines":["bad duration"],"holdMs":30001}',
    '{"type":"reply","lines":["bad duration"],"holdMs":1000.5}',
    '{"type":"reply","lines":["bad duration"],"holdMs":"1000"}',
    '{"type":"reply","lines":["bad duration"],"holdMs":null}',
    '{"type":"reply","lines":["bad duration"],"holdMs":true}',
    $oversizedPage,
    '{"type":"reply","lines":["one"],"text":"two"}',
    '{"type":"status","text":"still connected"}',
    $twentyJoinedEmojiLines,
    $twentyJoinedEmojiText,
    '{"type":"reply","text":"  <silent/>\r\n"}',
    '{"type":"reply","text":"\u4f60\u597d","holdMs":30000}',
    '{"type":"clear"}',
    '{"type":"reply","lines":["one","two","three","four"]}',
    '{"type":"hide"}',
    '{"type":"reply","text":"hidden discarded"}',
    '{"type":"show"}',
    '{"type":"shutdown"}'
)
$protocol.Output | Set-Content -LiteralPath (Join-Path $testDirectory 'protocol-result.jsonl') -Encoding UTF8
$protocol.Error | Set-Content -LiteralPath (Join-Path $testDirectory 'protocol-stderr.txt') -Encoding UTF8
if ($protocol.ExitCode -ne 0) { throw 'Protocol process failed.' }
$events = @($protocol.Output -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
if ($events[0].type -ne 'ready' -or $events[0].mode -ne 'smoke' -or $events[0].protocolVersion -ne 3) { throw 'Missing version 3 ready event.' }
$handled = @($events | Where-Object type -eq 'handled')
if ($handled.Count -ne 13) { throw ('Unexpected handled count: ' + $handled.Count) }
if ($handled[0].replyLength -ne $ordinary.Length -or $handled[0].silent) { throw 'Literal marker was incorrectly filtered.' }
if ($handled[0].holdMs -ne 5000) { throw 'Default hold duration changed.' }
if ($handled[1].replyLength -ne 3 -or $handled[1].pendingLines -ne 2 -or $handled[1].holdMs -ne 500) { throw 'Three-page sequence or custom hold duration was not retained.' }
if ($handled[2].replyLength -ne 3 -or $handled[2].pendingLines -ne 2) { throw 'Status replaced the character reply.' }
if ($handled[3].replyLength -ne 3 -or $handled[3].pendingLines -ne 2 -or $handled[3].holdMs -ne 500) { throw 'Malformed input replaced the active sequence.' }
if ($handled[4].replyLength -ne 100 -or $handled[5].replyLength -ne 100) { throw 'Twenty valid ZWJ emoji were rejected or truncated.' }
if (-not $handled[6].silent -or $handled[6].pendingLines -ne 0) { throw 'Exact silent marker did not clear the sequence.' }
if ($handled[7].replyLength -ne 2 -or $handled[7].holdMs -ne 30000) { throw 'Unicode or maximum hold duration was lost.' }
if (-not $handled[8].silent -or $handled[8].pendingLines -ne 0) { throw 'Clear did not cancel the sequence.' }
if ($handled[9].pendingLines -ne 3) { throw 'Fourth page was dropped.' }
if (-not $handled[10].silent -or -not $handled[11].silent -or -not $handled[12].silent) { throw 'Hide did not cancel, hidden reply was played, or show replayed stale text.' }
$invalid = @($events | Where-Object type -eq 'invalid')
if ($invalid.Count -ne 13) { throw ('Unexpected invalid count: ' + $invalid.Count) }
if (@($invalid | Where-Object code -eq 'REPLY_TOO_LONG').Count -ne 1) { throw 'Long legacy replies were not explicitly rejected.' }
if (@($invalid | Where-Object code -eq 'INVALID_HOLD_MS').Count -ne 6) { throw 'Invalid hold duration was accepted.' }
if (@($invalid | Where-Object code -eq 'REPLY_TRANSPORT_LIMIT').Count -ne 1) { throw 'Oversized transport page was accepted.' }
$playback = @($events | Where-Object type -eq 'playback')
$playbackFlags = ($playback | ForEach-Object { if ($_.active) { '1' } else { '0' } }) -join ''
if ($playbackFlags -ne '11110101000') { throw ('Replacement/cancellation playback notifications incorrect: ' + $playbackFlags) }
if ($events[-1].reason -ne 'shutdown') { throw 'Explicit shutdown was not honored.' }
if (-not $protocol.Error.Contains('protocol failed')) { throw 'Invalid input was not reported to stderr.' }

# Correlation tags let the bridge ignore an older clear acknowledgement after it
# has already submitted a reply. Invalid replies report the still-playing command.
$correlated = Invoke-Renderer '--smoke-test' @(
    '{"type":"clear","commandId":"1"}',
    '{"type":"reply","commandId":"2","lines":["first","second","last"],"holdMs":30000}',
    '{"type":"reply","commandId":"3","lines":["invalid hold"],"holdMs":499}',
    '{"type":"reply","commandId":"4","lines":["\ud800"]}',
    '{"type":"status","text":"still displaying command 2"}',
    '{"type":"clear","commandId":"5"}',
    '{"type":"reply","commandId":"6","text":"hide this"}',
    '{"type":"hide","commandId":"7"}',
    '{"type":"reply","commandId":"8","text":"hidden discarded"}',
    '{"type":"show"}',
    '{"type":"reply","commandId":"9","text":"replacement one"}',
    '{"type":"reply","commandId":"10","text":"replacement two"}',
    '{"type":"shutdown"}'
)
$correlated.Output | Set-Content -LiteralPath (Join-Path $testDirectory 'correlation-result.jsonl') -Encoding UTF8
$correlatedEvents = @($correlated.Output -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
if ($correlated.ExitCode -ne 0) { throw 'Correlated protocol process failed.' }
$correlatedPlayback = @($correlatedEvents | Where-Object type -eq 'playback')
$correlatedFlags = ($correlatedPlayback | ForEach-Object { $_.commandId + ':' + [int][bool]$_.active }) -join ','
if ($correlatedFlags -ne '1:0,2:1,5:0,6:1,7:0,8:0,9:1,10:1,10:0') { throw ('Display acknowledgements lost their command identity: ' + $correlatedFlags) }
$correlatedInvalid = @($correlatedEvents | Where-Object type -eq 'invalid')
if ($correlatedInvalid.Count -ne 2 -or $correlatedInvalid[0].commandId -ne '3' -or $correlatedInvalid[1].commandId -ne '4') { throw 'Invalid replies did not identify the rejected command.' }
foreach ($entry in $correlatedInvalid) {
    if (-not $entry.active -or $entry.currentCommandId -ne '2') { throw 'Invalid reply lost the existing playback state.' }
}
$preserved = $correlatedEvents | Where-Object { $_.type -eq 'handled' -and $_.messageType -eq 'status' } | Select-Object -First 1
if ($preserved.commandId -ne '2' -or $preserved.pendingLines -ne 2 -or $preserved.holdMs -ne 30000) { throw 'Rejected command changed the existing sequence.' }

$eof = Invoke-Renderer '--smoke-test' @('{"type":"reply","text":"EOF test"}')
$eof.Output | Set-Content -LiteralPath (Join-Path $testDirectory 'eof-result.jsonl') -Encoding UTF8
$eofEvents = @($eof.Output -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
if ($eof.ExitCode -ne 0 -or $eof.Error -or $eofEvents[-1].reason -ne 'stdin-closed') { throw 'Parent EOF shutdown failed.' }
if (($eofEvents | Where-Object type -eq 'playback' | Select-Object -Last 1).active) { throw 'Parent EOF did not clear playback.' }

# Exercise the real DispatcherTimer and stdout end notification as well as the
# deterministic render-state timeline, with stdin left open until pages finish.
$natural = Invoke-Renderer '--smoke-test' @('{"type":"reply","commandId":"natural-1","lines":["first","second","last"],"holdMs":500}') 4500
$natural.Output | Set-Content -LiteralPath (Join-Path $testDirectory 'natural-end-result.jsonl') -Encoding UTF8
$naturalEvents = @($natural.Output -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
$naturalPlayback = @($naturalEvents | Where-Object type -eq 'playback')
if ($natural.ExitCode -ne 0 -or $natural.Error -or $naturalPlayback.Count -ne 3 -or -not $naturalPlayback[0].active -or $naturalPlayback[1].active -or $naturalPlayback[2].active) { throw 'Natural completion did not report idle before parent EOF.' }
if (@($naturalPlayback | Where-Object commandId -ne 'natural-1').Count) { throw 'Natural completion or exit lost the current command ID.' }

$preferences = Invoke-Renderer '--smoke-test' @(
    '{"type":"reply","commandId":"display-1","lines":["keep playing"],"holdMs":30000}',
    '{"type":"preferences","bindingId":"session-A:1","values":{"decisionIntervalMs":15000,"pollIntervalMs":1000,"bubbleHoldMs":5000,"replyLength":20,"dataMode":"detailed"}}',
    '{"type":"preferences","bindingId":"session-A:1","values":{"decisionIntervalMs":500,"pollIntervalMs":500,"bubbleHoldMs":1000,"replyLength":0,"dataMode":"basic"}}',
    '{"type":"preferences","bindingId":"session-A:1","values":{"decisionIntervalMs":499,"pollIntervalMs":500,"bubbleHoldMs":1000,"replyLength":0,"dataMode":"basic"}}',
    '{"type":"status","text":"still has valid preferences"}',
    '{"type":"preferences","bindingId":null}',
    '{"type":"shutdown"}'
)
$preferences.Output | Set-Content -LiteralPath (Join-Path $testDirectory 'preferences-result.jsonl') -Encoding UTF8
$preferenceEvents = @($preferences.Output -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
if ($preferences.ExitCode -ne 0) { throw 'Preferences protocol process failed.' }
$preferenceHandled = @($preferenceEvents | Where-Object { $_.type -eq 'handled' -and $_.messageType -eq 'preferences' })
if ($preferenceHandled.Count -ne 3 -or $preferenceHandled[0].bindingId -ne 'session-A:1' -or $preferenceHandled[0].dataMode -ne 'detailed' -or $preferenceHandled[1].decisionIntervalMs -ne 500 -or $preferenceHandled[1].pollIntervalMs -ne 500 -or $null -ne $preferenceHandled[2].bindingId) { throw 'Host preferences were not accepted or unbound.' }
$preferenceInvalid = @($preferenceEvents | Where-Object type -eq 'invalid')
if ($preferenceInvalid.Count -ne 1 -or $preferenceInvalid[0].code -ne 'INVALID_PREFERENCES' -or -not $preferenceInvalid[0].active -or $preferenceInvalid[0].currentCommandId -ne 'display-1') { throw 'Malformed settings changed playback.' }
$preferenceStatus = $preferenceEvents | Where-Object { $_.type -eq 'handled' -and $_.messageType -eq 'status' } | Select-Object -First 1
if ($preferenceStatus.decisionIntervalMs -ne 500 -or $preferenceStatus.dataMode -ne 'basic') { throw 'Malformed preferences changed accepted values.' }
if (@($preferenceEvents | Where-Object type -eq 'playback').Count -ne 2) { throw 'Settings snapshots altered playback state.' }

Write-Output ('PASS: conversation settings UI, correlated preference requests, custom/0.5-second intervals, acknowledgement/error/rebind handling, unchanged playback protocol, 257 ordered pages, configurable hold, real timer completion, character alpha, compact layout, strict malformed validation, Unicode and parent EOF. Preview: ' + $preview)
