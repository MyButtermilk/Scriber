function Protect-SmokeDiagnosticText {
    param([string]$Text, [string]$Token)

    $safe = $Text.Replace([string][char]0, "")
    # Backend stderr is not guaranteed to have passed through runtime redaction.
    # Replace actual inherited secrets as well as recognizable assignments.
    $secrets = @($Token) + @(
        Get-ChildItem Env: | Where-Object {
            $_.Name -match '(?i)(key|token|secret|password|credential|authorization|cookie|session)'
        } | ForEach-Object { [string]$_.Value }
    )
    foreach ($secret in @($secrets | Where-Object { $_ } | Sort-Object Length -Descending -Unique)) {
        $safe = $safe.Replace($secret, '[REDACTED]')
    }
    $safe = [regex]::Replace($safe, '(?im)^.*(?:api[_-]?key|speech[_-]?key|token|secret|password|credential|authorization|cookie|session)[\w"''-]*\s*[:=].*$', '[REDACTED_SECRET_LINE]')
    $safe = [regex]::Replace($safe, '(?i)\b(?:Bearer|Token)\s+[^\s"'']+', '[REDACTED_AUTH]')
    $safe = [regex]::Replace($safe, '\b(?:sk-|gsk_|ck_|AIza)[A-Za-z0-9_-]{8,}', '[REDACTED]')
    $safe = [regex]::Replace($safe, '(?i)SWD(?:\\+|#)+MMDEVAPI[^\s"'',;<>]*', '[REDACTED_ENDPOINT]')
    $safe = [regex]::Replace($safe, '(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n"'']*', '[REDACTED_PATH]')
    $safe = [regex]::Replace($safe, '(?<![\w:])/(?:home|Users|tmp|var|private|workspace|workspaces)/[^\r\n"'']*', '[REDACTED_PATH]')
    return $safe
}

function Get-SmokeStartupFailureDiagnostics {
    param(
        [string]$RuntimeDataDir,
        [System.Diagnostics.Process]$AppProcess,
        [ValidateRange(0, 2147483647)]
        [Nullable[int]]$ManagedBackendCount = $null,
        [string]$Token
    )

    $appState = [ordered]@{ started = ($null -ne $AppProcess); hasExited = $null; exitCode = $null }
    if ($null -ne $AppProcess) {
        try {
            $AppProcess.Refresh()
            $appState.hasExited = [bool]$AppProcess.HasExited
            if ($appState.hasExited) { $appState.exitCode = $AppProcess.ExitCode }
        } catch {
            $appState.error = 'process_state_unavailable'
        }
    }

    $logs = @()
    foreach ($name in @(
        'tauri-shell.log', 'tauri-backend.log', 'backend-crash-metadata.jsonl',
        'smoke-shell.stdout.log', 'smoke-shell.stderr.log'
    )) {
        $entry = [ordered]@{ name = $name; present = $false; truncated = $false; text = '' }
        $stream = $null
        try {
            $path = Join-Path (Join-Path $RuntimeDataDir 'logs') $name
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $entry.present = $true
                # Bounded byte read even if a broken process floods stderr.
                $stream = [System.IO.File]::Open($path, 'Open', 'Read', 'ReadWrite, Delete')
                $entry.bytes = $stream.Length
                $offset = [Math]::Max(0, $stream.Length - 16384)
                $entry.truncated = $offset -gt 0
                [void]$stream.Seek($offset, [System.IO.SeekOrigin]::Begin)
                $buffer = New-Object byte[] 16384
                $count = $stream.Read($buffer, 0, $buffer.Length)
                $text = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $count)
                # Do not expose a secret suffix when the byte boundary split a line.
                if ($offset -gt 0) {
                    $newline = $text.IndexOf("`n")
                    $text = if ($newline -ge 0) { $text.Substring($newline + 1) } else { '' }
                }
                $safe = Protect-SmokeDiagnosticText -Text $text -Token $Token
                $entry.text = $safe.Substring(0, [Math]::Min(16384, $safe.Length))
            }
        } catch {
            $entry.error = 'log_read_failed'
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
        $logs += [pscustomobject]$entry
    }
    return [pscustomobject]@{
        capturedBeforeCleanup = $true
        app = [pscustomobject]$appState
        managedBackendCount = $ManagedBackendCount
        logs = $logs
    }
}

Export-ModuleMember -Function Get-SmokeStartupFailureDiagnostics
