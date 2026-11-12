$ErrorActionPreference = "Stop"

# JobApplier-Core Windows Git hook bootstrapper.
# Copies scripts/hooks/pre-commit to .git/hooks/pre-commit (preserving LF endings),
# sets executable permissions where supported, and optionally runs `pre-commit install`.

$repoRoot = $null
try {
    $gitRoot = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($gitRoot)) {
        $repoRoot = $gitRoot.Trim()
    }
} catch {
    $repoRoot = $null
}

if (-not $repoRoot) {
    $repoRoot = Split-Path -Parent $PSScriptRoot
}

$sourceHook = Join-Path $repoRoot "scripts\hooks\pre-commit"
$hooksDir = Join-Path $repoRoot ".git\hooks"
$targetHook = Join-Path $hooksDir "pre-commit"

if (-not (Test-Path -LiteralPath $sourceHook)) {
    throw "Canonical hook script not found at: $sourceHook"
}

if (-not (Test-Path -LiteralPath $hooksDir)) {
    New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null
}

# Preserve LF line endings regardless of git core.autocrlf settings
$rawContent = [System.IO.File]::ReadAllText($sourceHook)
$lfContent = $rawContent -replace "`r`n", "`n" -replace "`r", "`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($targetHook, $lfContent, $utf8NoBom)

# Ensure executable permissions via Git Bash chmod +x if available
$bashCandidates = @(
    "C:\Program Files\Git\bin\bash.exe",
    "C:\Program Files (x86)\Git\bin\bash.exe"
)

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if ($gitCmd) {
    $gitDir = Split-Path -Parent $gitCmd.Source
    $gitInstallRoot = Split-Path -Parent $gitDir
    $bashCandidates = @(
        (Join-Path $gitInstallRoot "bin\bash.exe"),
        (Join-Path $gitInstallRoot "usr\bin\bash.exe")
    ) + $bashCandidates
}

$gitBash = $bashCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if ($gitBash) {
    $posixTarget = $targetHook -replace "\\", "/"
    & $gitBash -c "chmod +x '$posixTarget'"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "chmod +x via Git Bash returned exit code $LASTEXITCODE."
    }
} else {
    Write-Warning "Git Bash not found; skipped chmod +x on $targetHook."
}

# Verify tracked scripts/hooks/pre-commit executable bit in git index if already tracked
$trackedCheck = & git -C $repoRoot ls-files --stage "scripts/hooks/pre-commit" 2>$null
if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($trackedCheck)) {
    if ($trackedCheck -notmatch "^100755\s") {
        & git -C $repoRoot update-index --chmod=+x "scripts/hooks/pre-commit" 2>$null
    }
}

# Optionally install pre-commit framework hooks if pre-commit CLI is available
$preCommitCmd = Get-Command pre-commit -ErrorAction SilentlyContinue
if ($preCommitCmd) {
    Push-Location $repoRoot
    try {
        & $preCommitCmd.Source install
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Installed pre-commit framework hooks via 'pre-commit install'."
        } else {
            Write-Warning "'pre-commit install' exited with code $LASTEXITCODE; native .git/hooks/pre-commit remains installed."
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "pre-commit CLI not installed (optional); using native .git/hooks/pre-commit."
}

Write-Host "Successfully installed JobApplier-Core pre-commit hook to: $targetHook"
