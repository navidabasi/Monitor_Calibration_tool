# Start Monitor calibration on Windows. Works on a machine without Python.
#
#   irm https://raw.githubusercontent.com/navidabasi/Monitor_Calibration_tool/main/run.ps1 | iex
#
# If uv isn't installed, it installs it first (from astral.sh, into your user
# folder, no admin rights needed). uv then downloads its own private copy of
# Python if needed and starts the app. Nothing else on the system is changed.
#
# MONCAL_SOURCE overrides where the app comes from, for example
#   $env:MONCAL_SOURCE = "monitor-calibration"      (the PyPI package)

# Everything runs inside a script block: when piped into `iex` this code runs in
# the user's own session, so it must not leave settings behind or call `exit`
# (which would close their PowerShell window).
& {
    $ErrorActionPreference = 'Stop'
    # Until the first PyPI release this is the repository archive (no git needed). Afterwards, change the default to
    # 'monitor-calibration'.
    $source = if ($env:MONCAL_SOURCE) { $env:MONCAL_SOURCE } else { 'https://github.com/navidabasi/Monitor_Calibration_tool/archive/refs/heads/main.zip' }

    function Find-Uvx {
        $cmd = Get-Command uvx -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        foreach ($dir in @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
            if (Test-Path "$dir\uvx.exe") { return "$dir\uvx.exe" }
        }
        return $null
    }

    $uvx = Find-Uvx
    if (-not $uvx) {
        Write-Host 'Installing uv, a small tool that runs Python apps (from astral.sh)...'
        powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        $uvx = Find-Uvx
        if (-not $uvx) {
            Write-Host 'uv was installed but uvx was not found. Open a new terminal and run: uvx monitor-calibration'
            return
        }
    }

    # Python 3.13 on every system: uv would otherwise pick the newest Python it can download, which the RAW reader
    # (rawpy) has no ready-made build for yet.
    & $uvx --python 3.13 --from $source monitor-calibration @args
} @args
