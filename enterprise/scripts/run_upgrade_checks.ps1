[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Before', 'After', 'Rollback')]
    [string]$Phase,
    [string]$PythonPath,
    [string]$NodePath,
    [string]$ArtifactRoot,
    [string]$BaselineSummary
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Runner = Join-Path $RepoRoot 'enterprise\scripts\run_enterprise_tests.ps1'
$StartedUtc = [DateTime]::UtcNow
$RunStamp = $StartedUtc.ToString('yyyyMMddTHHmmssZ')
if ([string]::IsNullOrWhiteSpace($ArtifactRoot)) {
    $ArtifactRoot = Join-Path $RepoRoot 'artifacts\upgrade-checks'
}
$RunDir = Join-Path $ArtifactRoot ("{0}-{1}-{2}" -f $RunStamp, $PID, $Phase.ToLowerInvariant())
$SummaryPath = Join-Path $RunDir 'summary.json'
$LogPath = Join-Path $RunDir 'upgrade-checks.log'

function Get-PowerShellPath {
    try {
        $current = (Get-Process -Id $PID -ErrorAction Stop).Path
        if (-not [string]::IsNullOrWhiteSpace($current) -and (Test-Path -LiteralPath $current -PathType Leaf)) {
            return $current
        }
    } catch {
        # Fall through to a command lookup for hosts that hide process paths.
    }
    foreach ($name in @('pwsh', 'powershell')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if ($null -ne $command) { return $command.Source }
    }
    throw 'PowerShell executable was not found'
}

function Set-HighestExitCode {
    param([int]$Code)
    $priority = @{ 0 = 0; 1 = 1; 2 = 2; 3 = 3; 4 = 4 }
    if (-not $priority.ContainsKey($Code)) { $Code = 4 }
    if ($priority[$Code] -gt $priority[$script:ExitCode]) {
        $script:ExitCode = $Code
    }
}

function Invoke-RunnerProfile {
    param([string]$Profile)
    $shell = Get-PowerShellPath
    $profileArtifact = Join-Path $RunDir $Profile.ToLowerInvariant()
    $arguments = @(
        '-NoProfile',
        '-NonInteractive',
        '-File',
        $Runner,
        '-Profile',
        $Profile,
        '-ArtifactRoot',
        $profileArtifact
    )
    if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
        $arguments += @('-PythonPath', $PythonPath)
    }
    if (-not [string]::IsNullOrWhiteSpace($NodePath)) {
        $arguments += @('-NodePath', $NodePath)
    }

    Add-Content -LiteralPath $LogPath -Value ("START profile={0}" -f $Profile) -Encoding utf8
    & $shell @arguments 2>&1 | ForEach-Object {
        Add-Content -LiteralPath $LogPath -Value $_.ToString() -Encoding utf8
    }
    $code = [int]$LASTEXITCODE
    Add-Content -LiteralPath $LogPath -Value ("END profile={0} exit={1}" -f $Profile, $code) -Encoding utf8
    Set-HighestExitCode -Code $code
    return [pscustomobject]@{
        profile = $Profile
        exitCode = $code
        artifactRoot = $profileArtifact
    }
}

function Get-CurrentCommit {
    $commit = @(& git -c core.excludesFile= -C $RepoRoot rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or $commit.Count -eq 0) {
        throw 'unable to resolve current git commit'
    }
    return $commit[0].ToString().Trim()
}

function Get-VersionManifestHash {
    $manifest = Join-Path $RepoRoot 'version-manifest.json'
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw 'version-manifest.json is missing'
    }
    return (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Read-Baseline {
    if ([string]::IsNullOrWhiteSpace($BaselineSummary)) {
        throw 'After/Rollback requires -BaselineSummary from a successful Before check'
    }
    if (-not (Test-Path -LiteralPath $BaselineSummary -PathType Leaf)) {
        throw 'baseline summary does not exist'
    }
    try {
        $baseline = Get-Content -LiteralPath $BaselineSummary -Raw -Encoding utf8 | ConvertFrom-Json
    } catch {
        throw 'baseline summary is not valid JSON'
    }
    if ($baseline.phase -ne 'before' -or $baseline.exitCode -ne 0) {
        throw 'baseline Contract/P0 checks did not pass'
    }
    return $baseline
}

$ExitCode = 0
$Profiles = [System.Collections.Generic.List[object]]::new()
$Baseline = $null
$Result = 'not_accepted'

try {
    New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
    if (-not (Test-Path -LiteralPath $RunDir -PathType Container)) {
        throw 'upgrade-check artifact directory was not created'
    }
    if ($Phase -ne 'Before') {
        $Baseline = Read-Baseline
    }
    $Profiles.Add((Invoke-RunnerProfile -Profile 'Contract'))
    $Profiles.Add((Invoke-RunnerProfile -Profile 'P0'))
    if ($ExitCode -eq 0) {
        $Result = if ($Phase -eq 'Before') { 'baseline_accepted' } else { 'accepted' }
    }
} catch {
    $message = $_.Exception.Message
    if ($message -like '*requires -BaselineSummary*' -or
        $message -like '*baseline summary*' -or
        $message -like '*baseline Contract/P0*') {
        Set-HighestExitCode -Code 2
    } else {
        Set-HighestExitCode -Code 4
    }
    Add-Content -LiteralPath $LogPath -Value 'upgrade check precondition failed' -Encoding utf8
} finally {
    try {
        $summary = [ordered]@{
            schemaVersion = 1
            phase = $Phase.ToLowerInvariant()
            result = $Result
            exitCode = $ExitCode
            exitCodeMeaning = [ordered]@{
                '0' = 'accepted'
                '1' = 'Contract or P0 failure'
                '2' = 'upgrade precondition or evidence blocked'
                '3' = 'external environment unavailable; preserve runner exit 3'
                '4' = 'runner/report/guard failure'
            }
            startedAt = $StartedUtc.ToString('o')
            finishedAt = [DateTime]::UtcNow.ToString('o')
            gitCommit = Get-CurrentCommit
            versionManifestSha256 = Get-VersionManifestHash
            baselineSummary = $BaselineSummary
            profiles = @($Profiles)
            artifacts = [ordered]@{
                log = $LogPath
                summary = $SummaryPath
            }
        }
        $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $SummaryPath -Encoding utf8
    } catch {
        $ExitCode = 4
        [Console]::Error.WriteLine('Unable to write upgrade-check summary')
    }
}

exit $ExitCode
