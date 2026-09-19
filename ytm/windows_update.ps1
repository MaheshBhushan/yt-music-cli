# Copied to a temporary directory before launch; never imports the ytm package.
param([string] $PlanPath)

$ErrorActionPreference = 'Stop'

function Wait-YtmExit($ParentId) {
    $parent = Get-Process -Id $ParentId -ErrorAction SilentlyContinue
    if ($null -ne $parent) {
        try {
            if (-not $parent.WaitForExit(30000)) {
                throw 'The original ytm process has not exited. Automatic update cancelled.'
            }
        } finally {
            $parent.Dispose()
        }
    }
}

function Test-YtmLaunchers($Paths) {
    foreach ($path in $Paths) {
        if ([System.IO.File]::Exists($path)) {
            # Opening without truncation or sharing detects running wrappers,
            # including the ytm.exe parent that can briefly outlive Python.
            $stream = [System.IO.File]::Open($path, 'Open', 'ReadWrite', 'None')
            $stream.Dispose()
        }
    }
}

function Wait-YtmLaunchers($Paths) {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($true) {
        try {
            Test-YtmLaunchers $Paths
            return
        } catch {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "A launcher is still locked or inaccessible. Close other ytm/yt-dlp instances and retry. No processes were stopped. $($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds 250
        }
    }
}

function Invoke-YtmCommand($Command) {
    $installerArgs = @($Command.arguments)
    & $Command.executable @installerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit $LASTEXITCODE): $($Command.executable)"
    }
}

function Invoke-YtmUpdate($Plan) {
    Write-Host 'Waiting for ytm to exit. Close other ytm instances; do not reopen ytm during the update.'
    Wait-YtmExit $Plan.parent_id
    Wait-YtmLaunchers $Plan.launchers
    Write-Host 'Updating ytm and yt-dlp...'
    foreach ($command in $Plan.commands) {
        Invoke-YtmCommand $command
    }
    # A fresh interpreter verifies the environment after pipx/uv replaced it.
    # Missing metadata, an older version, or a failed interpreter is a failure.
    $verify = @'
from importlib.metadata import version
import re, sys
installed = version('ytm')
target = sys.argv[1]
parts = lambda v: tuple(int(p) if p.isdigit() else -1 for p in re.split(r'[.+-]', v))
print('Installed ytm ' + installed)
if target and parts(installed) < parts(target):
    sys.exit('Expected at least ytm ' + target + '; the package index may not have caught up. Try again later.')
'@
    # A sentinel avoids PowerShell 5.1 dropping empty native arguments.
    $expected = if ($Plan.target) { $Plan.target } else { '0' }
    Invoke-YtmCommand @{ executable = $Plan.python; arguments = @('-c', $verify, $expected) }
    Write-Host 'Successfully updated ytm and yt-dlp. You can now run ytm again.'
}

# With no plan, only define the functions (used by the PowerShell tests).
if ($PlanPath) {
    $exitCode = 1
    $plan = $null
    try {
        $plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Host "Manual fallback after closing all ytm instances:`n$($plan.fallback)"
        Invoke-YtmUpdate $plan
        $exitCode = 0
    } catch {
        Write-Host "Update failed: $($_.Exception.Message)"
        if ($null -ne $plan) {
            Write-Host "Run manually after closing all ytm instances:`n$($plan.fallback)"
        }
    } finally {
        # The script and plan live together in a private temporary directory.
        Remove-Item -LiteralPath $PlanPath, $PSCommandPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $PSScriptRoot -ErrorAction SilentlyContinue
    }
    Read-Host 'Press Enter to close this window' | Out-Null
    exit $exitCode
}
