[CmdletBinding()]
param(
    [string]$Profile = 'P0',
    [string]$PythonPath,
    [string]$NodePath,
    [string]$ArtifactRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

class DependencyException : System.Exception {
    DependencyException([string]$message) : base($message) {}
}

class ExternalEnvironmentException : System.Exception {
    ExternalEnvironmentException([string]$message) : base($message) {}
}

$AllowedProfiles = @('Contract', 'P0', 'Integration', 'WP03', 'All')
if ($AllowedProfiles -notcontains $Profile) {
    [Console]::Error.WriteLine("Invalid profile '$Profile'. Allowed profiles: $($AllowedProfiles -join ', ')")
    exit 4
}
$Profile = @($AllowedProfiles | Where-Object { $_ -ieq $Profile })[0]

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$WebRoot = Join-Path $RepoRoot 'enterprise\web'
$StartedUtc = [DateTime]::UtcNow
$RunStamp = $StartedUtc.ToString('yyyyMMddTHHmmssZ')
if ([string]::IsNullOrWhiteSpace($ArtifactRoot)) {
    $ArtifactRoot = Join-Path $RepoRoot 'artifacts\enterprise-tests'
} elseif (-not [System.IO.Path]::IsPathRooted($ArtifactRoot)) {
    $ArtifactRoot = Join-Path $RepoRoot $ArtifactRoot
}
$ArtifactRoot = [System.IO.Path]::GetFullPath($ArtifactRoot)
$RunArtifactDir = Join-Path $ArtifactRoot ("{0}-{1}" -f $RunStamp, $PID)
$JUnitDir = Join-Path $RunArtifactDir 'junit'
$LogPath = Join-Path $RunArtifactDir 'runner.log'
$SummaryPath = Join-Path $RunArtifactDir 'summary.json'
$AcceptancePath = Join-Path $RunArtifactDir 'acceptance.md'
$AggregateJUnitPath = Join-Path $JUnitDir 'acceptance.xml'
$RunTempDir = Join-Path $RunArtifactDir 'temp'

try {
    New-Item -ItemType Directory -Path $JUnitDir -Force | Out-Null
    New-Item -ItemType Directory -Path $RunTempDir -Force | Out-Null
    if (-not (Test-Path -LiteralPath $JUnitDir -PathType Container) -or
        -not (Test-Path -LiteralPath $RunTempDir -PathType Container)) {
        throw 'artifact directories were not created'
    }
} catch {
    [Console]::Error.WriteLine("Unable to create acceptance artifact directory: $($_.Exception.Message)")
    exit 4
}
$Steps = [System.Collections.Generic.List[object]]::new()
$DesiredExitCode = 0
$PythonRuntime = $null
$NodeRuntime = $null
$RagflowBefore = @()
$TrackedBefore = @()
$GitCommit = $null
$WorktreeDirty = $false
$RagflowAfter = @()
$RagflowGuardUnchanged = $false
$LocationPushed = $false

function Redact-SensitiveText {
    param([AllowNull()][string]$Text)
    if ($null -eq $Text) { return '' }
    $redacted = $Text
    foreach ($name in @(
        'ENTERPRISE_RAGFLOW_API_KEY',
        'ENTERPRISE_ASSET_REGISTRY_TOKEN',
        'ENTERPRISE_SYNC_SERVICE_TOKEN',
        'ENTERPRISE_SYNC_HMAC_CREDENTIALS',
        'ENTERPRISE_E2E_HMAC_SECRET',
        'WP03_UNAUTHORIZED_USER_TOKEN',
        'ENTERPRISE_REDIS_URL',
        'RAGFLOW_API_KEY',
        'JWT_SHARED_SECRET',
        'S3_ACCESS_KEY',
        'S3_SECRET_KEY',
        'RAGFLOW_ADMIN_PASSWORD',
        'Authorization',
        'Cookie'
    )) {
        $secret = [Environment]::GetEnvironmentVariable($name)
        if (-not [string]::IsNullOrWhiteSpace($secret)) {
            $redacted = $redacted.Replace($secret, '<redacted>')
        }
    }
    $redacted = [regex]::Replace(
        $redacted,
        '(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)[^\s,;]+',
        '$1<redacted>'
    )
    $redacted = [regex]::Replace(
        $redacted,
        '(?i)(["'']?(?:token|password|secret|api[_-]?key|accessToken|refreshToken|cookie|authorization)["'']?\s*[:=]\s*["'']?)[^"''&\s,}]+',
        '$1<redacted>'
    )
    return $redacted
}

function Get-GitCommit {
    $previousErrorAction = $ErrorActionPreference
    $code = 0
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        $commitLines = @(& git -c core.excludesFile= -C $RepoRoot rev-parse HEAD 2>$null)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($code -ne 0 -or $commitLines.Count -eq 0) {
        throw 'unable to resolve current git commit'
    }
    return $commitLines[0].ToString().Trim()
}

function Get-WorktreeState {
    $previousErrorAction = $ErrorActionPreference
    $code = 0
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        $status = @(& git -c core.excludesFile= -C $RepoRoot status --porcelain=v1 --untracked-files=all 2>$null)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($code -ne 0) { throw 'unable to read worktree status' }
    return $status
}

function Write-Log {
    param([string]$Message)
    $safeMessage = Redact-SensitiveText -Text $Message
    $line = "{0} {1}" -f [DateTime]::UtcNow.ToString('o'), $safeMessage
    Write-Host $line
    Add-Content -LiteralPath $LogPath -Value $line -Encoding utf8
}

function Set-ExitCode {
    param([int]$Code)
    $priority = @{ 0 = 0; 1 = 1; 2 = 2; 3 = 3; 4 = 4 }
    if ($priority[$Code] -gt $priority[$script:DesiredExitCode]) {
        $script:DesiredExitCode = $Code
    }
}

function Add-Step {
    param(
        [string]$Name,
        [ValidateSet('passed', 'failed', 'blocked')]
        [string]$Status,
        [int]$ExitCode,
        [string]$Detail,
        [string]$JUnit = ''
    )
    $script:Steps.Add([pscustomobject]@{
        name = $Name
        status = $Status
        exitCode = $ExitCode
        detail = Redact-SensitiveText -Text $Detail
        junit = $JUnit
    })
}

function Resolve-ExplicitRuntime {
    param([string]$Value, [string]$RuntimeName)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    if (Test-Path -LiteralPath $Value -PathType Leaf) {
        $path = (Resolve-Path -LiteralPath $Value).Path
        if (-not (Test-RuntimeCandidate -Path $path)) {
            throw [DependencyException]::new("explicit $RuntimeName runtime cannot be executed")
        }
        return [pscustomobject]@{ path = $path; source = 'explicit' }
    }
    $command = Get-Command $Value -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        if (-not (Test-RuntimeCandidate -Path $command.Source)) {
            throw [DependencyException]::new("explicit $RuntimeName runtime cannot be executed")
        }
        return [pscustomobject]@{ path = $command.Source; source = 'explicit' }
    }
    throw [DependencyException]::new("explicit $RuntimeName runtime was not found")
}

function Test-RuntimeCandidate {
    param([string]$Path)
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        $null = & $Path '--version' 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function Get-CodexResourceRoots {
    $roots = [System.Collections.Generic.List[string]]::new()
    foreach ($process in @(Get-Process -Name codex -ErrorAction SilentlyContinue)) {
        try {
            if (-not [string]::IsNullOrWhiteSpace($process.Path)) {
                $roots.Add((Split-Path -Parent $process.Path))
            }
        } catch {
            # Some Codex helper processes do not expose their executable path.
        }
    }
    foreach ($candidate in @(
        [Environment]::GetEnvironmentVariable('CODEX_RUNTIME_ROOT'),
        [Environment]::GetEnvironmentVariable('CODEX_DEPENDENCY_ROOT'),
        (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cache\codex-runtimes\codex-primary-runtime\dependencies'),
        (Join-Path $RepoRoot '..\..\..\..\.cache\codex-runtimes\codex-primary-runtime\dependencies')
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Container)) {
            $roots.Add((Resolve-Path -LiteralPath $candidate).Path)
        }
    }
    return @($roots | Select-Object -Unique)
}

function Resolve-Runtime {
    param(
        [string]$Explicit,
        [string]$RuntimeName,
        [string[]]$PathNames,
        [string[]]$EnvironmentNames,
        [scriptblock]$BundledCandidates
    )
    $resolved = Resolve-ExplicitRuntime -Value $Explicit -RuntimeName $RuntimeName
    if ($null -ne $resolved) { return $resolved }

    foreach ($name in $PathNames) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command -and (Test-RuntimeCandidate -Path $command.Source)) {
            return [pscustomobject]@{ path = $command.Source; source = 'PATH' }
        }
    }

    foreach ($name in $EnvironmentNames) {
        $candidate = [Environment]::GetEnvironmentVariable($name)
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and
            (Test-Path -LiteralPath $candidate -PathType Leaf) -and
            (Test-RuntimeCandidate -Path $candidate)) {
            return [pscustomobject]@{ path = (Resolve-Path -LiteralPath $candidate).Path; source = 'bundled' }
        }
    }

    foreach ($candidate in @(& $BundledCandidates)) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and
            (Test-Path -LiteralPath $candidate -PathType Leaf) -and
            (Test-RuntimeCandidate -Path $candidate)) {
            return [pscustomobject]@{ path = (Resolve-Path -LiteralPath $candidate).Path; source = 'bundled' }
        }
    }
    throw [DependencyException]::new("$RuntimeName runtime was not found via explicit path, PATH, or bundled runtime")
}

function Invoke-Logged {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$Label
    )
    Write-Log "START $Label"
    & $FilePath @Arguments 2>&1 | ForEach-Object {
        $line = Redact-SensitiveText -Text $_.ToString()
        Write-Host $line
        Add-Content -LiteralPath $LogPath -Value $line -Encoding utf8
    }
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    Write-Log "END $Label exit=$code"
    return [int]$code
}

function Read-JUnitCounts {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    if ((Get-Item -LiteralPath $Path).Length -eq 0) { return $null }
    try {
        [xml]$document = Get-Content -LiteralPath $Path -Raw -Encoding utf8
    } catch {
        return $null
    }
    if ($null -eq $document) { return $null }
    $nodes = @($document.SelectNodes('//testsuite[not(testsuite)]'))
    if ($nodes.Count -eq 0 -and $null -ne $document.testsuite) {
        $nodes = @($document.testsuite)
    }
    $counts = @{ tests = 0; failures = 0; errors = 0; skipped = 0 }
    foreach ($node in $nodes) {
        foreach ($name in @('tests', 'failures', 'errors', 'skipped')) {
            $attribute = $node.Attributes[$name]
            if ($null -ne $attribute) { $counts[$name] += [int]$attribute.Value }
        }
    }
    return [pscustomobject]$counts
}

function Invoke-PytestStep {
    param(
        [string]$Name,
        [string[]]$TestArguments,
        [string]$JUnitName
    )
    $junit = Join-Path $JUnitDir $JUnitName
    $arguments = @(
        '-m', 'pytest'
    ) + $TestArguments + @(
        '-ra', '--tb=short', '--strict-markers',
        '-o', 'xfail_strict=true', '-p', 'no:cacheprovider',
        "--junitxml=$junit"
    )
    $savedEnvironment = @{}
    $applicationEnvironmentNames = @(
        Get-ChildItem Env: | Where-Object {
            $_.Name -match '^(ENTERPRISE_|RAGFLOW_|JWT_|S3_|PG_)' -or
            $_.Name -in @('AUTH_ENABLED', 'GATEWAY_URL', 'TYRAG_EXTERNAL_SOURCE_INTERNAL_KEY')
        } | ForEach-Object { $_.Name }
    )
    foreach ($environmentName in $applicationEnvironmentNames) {
        $savedEnvironment[$environmentName] = [Environment]::GetEnvironmentVariable($environmentName)
        [Environment]::SetEnvironmentVariable($environmentName, $null, 'Process')
    }
    [Environment]::SetEnvironmentVariable('ENTERPRISE_TEST_MODE', '1', 'Process')
    try {
        $code = Invoke-Logged -FilePath $PythonRuntime.path -Arguments $arguments -Label $Name
    } finally {
        [Environment]::SetEnvironmentVariable('ENTERPRISE_TEST_MODE', $null, 'Process')
        foreach ($environmentName in $savedEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable(
                $environmentName,
                $savedEnvironment[$environmentName],
                'Process'
            )
        }
    }
    $counts = Read-JUnitCounts -Path $junit
    if ($null -eq $counts) {
        Add-Step -Name $Name -Status failed -ExitCode 4 -Detail 'JUnit report was not produced' -JUnit $junit
        Set-ExitCode 4
        return
    }
    $detail = "tests=$($counts.tests) failures=$($counts.failures) errors=$($counts.errors) skipped=$($counts.skipped)"
    if ($code -ne 0 -or $counts.tests -eq 0 -or $counts.failures -gt 0 -or $counts.errors -gt 0 -or $counts.skipped -gt 0) {
        $stepCode = if ($code -in @(3, 4, 5)) { 4 } else { 1 }
        Add-Step -Name $Name -Status failed -ExitCode $stepCode -Detail $detail -JUnit $junit
        Set-ExitCode $stepCode
        return
    }
    Add-Step -Name $Name -Status passed -ExitCode 0 -Detail $detail -JUnit $junit
}

function Invoke-CommandStep {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments
    )
    $code = Invoke-Logged -FilePath $FilePath -Arguments $Arguments -Label $Name
    if ($code -eq 0) {
        Add-Step -Name $Name -Status passed -ExitCode 0 -Detail 'command completed'
    } else {
        Add-Step -Name $Name -Status failed -ExitCode $code -Detail 'command failed'
        Set-ExitCode 1
    }
}

function Invoke-WP03AcceptanceStep {
    $junit = Join-Path $JUnitDir 'wp03-acceptance.xml'
    $artifactDir = Join-Path $RunArtifactDir 'wp03'
    $script = Join-Path $RepoRoot 'enterprise\scripts\wp03\acceptance.py'
    $runId = "wp03-acceptance-$RunStamp-$PID"
    $arguments = @(
        $script,
        '--artifact-dir', $artifactDir,
        '--junit', $junit,
        '--run-id', $runId
    )
    $code = Invoke-Logged -FilePath $PythonRuntime.path -Arguments $arguments -Label 'wp03-formal-acceptance'
    $counts = Read-JUnitCounts -Path $junit
    if ($null -eq $counts) {
        Add-Step -Name wp03-formal-acceptance -Status failed -ExitCode $code -Detail 'JUnit report was not produced' -JUnit $junit
        if ($code -eq 2) { Set-ExitCode 2 }
        elseif ($code -eq 3) { Set-ExitCode 3 }
        else { Set-ExitCode 1 }
        return
    }
    $detail = "tests=$($counts.tests) failures=$($counts.failures) errors=$($counts.errors) skipped=$($counts.skipped) evidence=$artifactDir"
    if ($counts.tests -eq 0 -or $counts.skipped -gt 0) {
        Add-Step -Name wp03-formal-acceptance -Status failed -ExitCode $code -Detail $detail -JUnit $junit
        if ($code -eq 2) { Set-ExitCode 2 }
        elseif ($code -eq 3) { Set-ExitCode 3 }
        else { Set-ExitCode 1 }
    } elseif ($code -eq 2) {
        Add-Step -Name wp03-formal-acceptance -Status blocked -ExitCode 2 -Detail $detail -JUnit $junit
        Set-ExitCode 2
    } elseif ($code -eq 3) {
        Add-Step -Name wp03-formal-acceptance -Status blocked -ExitCode 3 -Detail $detail -JUnit $junit
        Set-ExitCode 3
    } elseif ($code -ne 0 -or $counts.failures -gt 0 -or $counts.errors -gt 0) {
        Add-Step -Name wp03-formal-acceptance -Status failed -ExitCode $code -Detail $detail -JUnit $junit
        Set-ExitCode 1
    } else {
        Add-Step -Name wp03-formal-acceptance -Status passed -ExitCode 0 -Detail $detail -JUnit $junit
    }
}

function Invoke-VitestStep {
    $junit = Join-Path $JUnitDir 'vitest.xml'
    $vitest = Join-Path $WebRoot 'node_modules\vitest\vitest.mjs'
    $arguments = @(
        $vitest, 'run', '--root', $WebRoot,
        '--config', (Join-Path $WebRoot 'vitest.config.ts'),
        '--reporter=default', '--reporter=junit', "--outputFile.junit=$junit"
    )
    $code = Invoke-Logged -FilePath $NodeRuntime.path -Arguments $arguments -Label 'vitest'
    $counts = Read-JUnitCounts -Path $junit
    if ($null -eq $counts) {
        Add-Step -Name vitest -Status failed -ExitCode 4 -Detail 'JUnit report was not produced' -JUnit $junit
        Set-ExitCode 4
        return
    }
    $detail = "tests=$($counts.tests) failures=$($counts.failures) errors=$($counts.errors) skipped=$($counts.skipped)"
    if ($code -ne 0 -or $counts.tests -eq 0 -or $counts.failures -gt 0 -or $counts.errors -gt 0 -or $counts.skipped -gt 0) {
        Add-Step -Name vitest -Status failed -ExitCode $code -Detail $detail -JUnit $junit
        Set-ExitCode 1
    } else {
        Add-Step -Name vitest -Status passed -ExitCode 0 -Detail $detail -JUnit $junit
    }
}

function Get-RagflowState {
    $previousErrorAction = $ErrorActionPreference
    $code = 0
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& git -c core.excludesFile= -C $RepoRoot status --porcelain=v1 --untracked-files=all -- ragflow 2>$null)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($code -ne 0) { throw 'unable to read ragflow git status' }
    return $output
}

function Get-EnvironmentValue {
    param([string]$Name)
    return [Environment]::GetEnvironmentVariable($Name)
}

function Test-UrlWithScheme {
    param(
        [string]$Value,
        [string[]]$Schemes
    )
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    $uri = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri)) { return $false }
    return $uri.Scheme -in $Schemes -and -not [string]::IsNullOrWhiteSpace($uri.Host)
}

function Set-IntegrationAliases {
    # Keep legacy client names process-local for the shared gateway libraries.
    # Values are never written to runner output or artifacts.
    [Environment]::SetEnvironmentVariable(
        'RAGFLOW_BASE_URL',
        (Get-EnvironmentValue 'ENTERPRISE_RAGFLOW_BASE_URL'),
        'Process'
    )
    [Environment]::SetEnvironmentVariable(
        'RAGFLOW_API_KEY',
        (Get-EnvironmentValue 'ENTERPRISE_RAGFLOW_API_KEY'),
        'Process'
    )
}

function Invoke-IntegrationProbeStep {
    $probe = Join-Path $RepoRoot 'enterprise\scripts\probe_integration_environment.py'
    if (-not (Test-Path -LiteralPath $probe -PathType Leaf)) {
        throw [DependencyException]::new('integration environment probe script is missing')
    }
    $code = Invoke-Logged -FilePath $PythonRuntime.path -Arguments @($probe) -Label 'integration-environment-probe'
    if ($code -eq 0) {
        Add-Step -Name integration-environment-probe -Status passed -ExitCode 0 `
            -Detail 'FILE_SHARE root, Asset Registry, RAGFlow, Redis/Valkey, Gateway, DB, and auth preflight passed'
    } elseif ($code -eq 3) {
        Add-Step -Name integration-environment-probe -Status blocked -ExitCode 3 `
            -Detail 'FILE_SHARE root, Asset Registry, RAGFlow, Redis/Valkey, Gateway, DB, or auth integration environment is unavailable'
        Set-ExitCode 3
        throw [ExternalEnvironmentException]::new(
            'FILE_SHARE root, Asset Registry, RAGFlow, Redis/Valkey, Gateway, DB, or auth integration environment is unavailable'
        )
    } elseif ($code -eq 4) {
        Add-Step -Name integration-environment-probe -Status failed -ExitCode 4 `
            -Detail 'integration environment probe tool failed'
        Set-ExitCode 4
    } else {
        Add-Step -Name integration-environment-probe -Status failed -ExitCode 1 `
            -Detail 'integration environment probe failed'
        Set-ExitCode 1
    }
}

function Assert-NoIntegrationBypassTests {
    $liveTests = @(
        'enterprise/scripts/run_file_share_v3_v2_e2e.py'
    )
    $bypassPattern = '(?im)pytest\.(?:skip|xfail)|pytest\.mark\.(?:skip|xfail)|unittest\.mock|from\s+unittest\s+import\s+mock|(?:MagicMock|AsyncMock|Mock)\('
    foreach ($relativePath in $liveTests) {
        $path = Join-Path $RepoRoot $relativePath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw [DependencyException]::new("required Integration test is missing: $relativePath")
        }
        $source = Get-Content -LiteralPath $path -Raw -Encoding utf8
        if ($source -match $bypassPattern) {
            throw [DependencyException]::new("Integration test contains skip/xfail/mock bypass: $relativePath")
        }
    }
}

function Invoke-RequiredIntegrationE2EStep {
    $script = Join-Path $RepoRoot 'enterprise\scripts\run_file_share_v3_v2_e2e.py'
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
        throw [DependencyException]::new('required FILE_SHARE/v2 E2E script is missing')
    }
    $report = Join-Path $RunArtifactDir 'file-share-v3-v2-e2e.json'
    $code = Invoke-Logged -FilePath $PythonRuntime.path -Arguments @($script, '--report', $report) -Label 'live-file-share-v3-v2'
    if ($code -eq 0) {
        Add-Step -Name live-file-share-v3-v2 -Status passed -ExitCode 0 -Detail 'FILE_SHARE v3 registration, server statusUrl polling, and formal v2 conversation E2E passed'
    } elseif ($code -eq 3) {
        Add-Step -Name live-file-share-v3-v2 -Status blocked -ExitCode 3 -Detail 'required FILE_SHARE/v2 E2E environment is unavailable'
        Set-ExitCode 3
    } else {
        $stepCode = if ($code -eq 4) { 4 } else { 1 }
        Add-Step -Name live-file-share-v3-v2 -Status failed -ExitCode $stepCode -Detail 'required FILE_SHARE/v2 E2E failed'
        Set-ExitCode $stepCode
    }
}

function Write-AggregateJUnit {
    $testCount = $Steps.Count
    $failureCount = @($Steps | Where-Object { $_.status -eq 'failed' }).Count
    $errorCount = @($Steps | Where-Object { $_.status -eq 'blocked' }).Count
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('<?xml version="1.0" encoding="utf-8"?>')
    $lines.Add("<testsuite name=`"enterprise-acceptance`" tests=`"$testCount`" failures=`"$failureCount`" errors=`"$errorCount`" skipped=`"0`" timestamp=`"$($StartedUtc.ToString('o'))`">")
    foreach ($step in $Steps) {
        $name = [Security.SecurityElement]::Escape($step.name)
        $detail = [Security.SecurityElement]::Escape($step.detail)
        $lines.Add("  <testcase classname=`"enterprise.acceptance`" name=`"$name`">")
        if ($step.status -eq 'failed') {
            $lines.Add("    <failure message=`"$detail`" />")
        } elseif ($step.status -eq 'blocked') {
            $lines.Add("    <error message=`"$detail`" />")
        }
        $lines.Add('  </testcase>')
    }
    $lines.Add('</testsuite>')
    Set-Content -LiteralPath $AggregateJUnitPath -Value $lines -Encoding utf8
}

function Write-Reports {
    $finishedUtc = [DateTime]::UtcNow
    $evidenceSummary = @($Steps | ForEach-Object {
        "{0}:{1}({2})" -f $_.name, $_.status, $_.exitCode
    }) -join '; '
    $offlineImplementationProfiles = @('P0', 'Integration', 'WP03', 'All')
    $offlineImplementationTestsRequested = $Profile -in $offlineImplementationProfiles
    $offlineImplementationStep = @($Steps | Where-Object { $_.name -eq 'pytest-offline' }) | Select-Object -First 1
    $offlineImplementationTestsExecuted = $null -ne $offlineImplementationStep
    $offlineImplementationTestStatus = if ($null -ne $offlineImplementationStep) {
        $offlineImplementationStep.status
    } elseif ($offlineImplementationTestsRequested) {
        'not_reached'
    } else {
        'not_requested'
    }
    $requiredIntegrationStep = @($Steps | Where-Object { $_.name -eq 'live-file-share-v3-v2' }) | Select-Object -First 1
    $requiredIntegrationEvidence = $null -ne $requiredIntegrationStep -and $requiredIntegrationStep.status -eq 'passed'
    $requiredIntegrationEvidenceReason = if ($requiredIntegrationEvidence) {
        'FILE_SHARE v3 + formal v2 live evidence was collected'
    } elseif ($null -eq $requiredIntegrationStep) {
        'FILE_SHARE v3 + formal v2 live suite was not reached'
    } else {
        'FILE_SHARE v3 + formal v2 live suite did not pass'
    }
    $summary = [ordered]@{
        profile = $Profile
        gitCommit = $GitCommit
        worktreeDirty = [bool]$WorktreeDirty
        passed = ($DesiredExitCode -eq 0)
        startedAt = $StartedUtc.ToString('o')
        finishedAt = $finishedUtc.ToString('o')
        success = ($DesiredExitCode -eq 0)
        exitCode = $DesiredExitCode
        exitCodeMeaning = [ordered]@{
            '0' = 'accepted'
            '1' = 'test or acceptance failure'
            '2' = 'formal acceptance blocked by missing real samples or local dependency'
            '3' = 'missing or invalid external integration environment'
            '4' = 'runner, report, or ragflow guard failure'
        }
        runtimes = [ordered]@{
            python = if ($null -eq $PythonRuntime) { $null } else { $PythonRuntime.source }
            node = if ($null -eq $NodeRuntime) { $null } else { $NodeRuntime.source }
        }
        liveTestsIncluded = ($Profile -in @('Integration', 'All'))
        requiredIntegrationEvidence = [bool]$requiredIntegrationEvidence
        requiredIntegrationEvidenceReason = $requiredIntegrationEvidenceReason
        offlineImplementationTestsExist = $true
        offlineImplementationTestsRequested = [bool]$offlineImplementationTestsRequested
        offlineImplementationTestsExecuted = [bool]$offlineImplementationTestsExecuted
        offlineImplementationTestStatus = $offlineImplementationTestStatus
        m3LiveIntegrationEvidence = $false
        m3LiveIntegrationEvidenceReason = 'Legacy M3/v1/S3/demo regression is outside the Required Integration evidence profile'
        persistence = [ordered]@{
            backend = 'sqlite'
            postgresIntegration = 'not_applicable'
            reason = 'no enterprise PostgreSQL runtime call path'
        }
        p1TestsRequested = [bool]$offlineImplementationTestsRequested
        p1Status = if ($offlineImplementationTestsExecuted) {
            'offline_implementation_tests_only'
        } elseif ($offlineImplementationTestsRequested) {
            'offline_implementation_tests_not_reached'
        } else {
            'not_requested'
        }
        evidenceSummary = $evidenceSummary
        evidence = [ordered]@{
            stepCount = $Steps.Count
            worktreeChangeCountBefore = $TrackedBefore.Count
            ragflowGuardUnchanged = [bool]$RagflowGuardUnchanged
            summary = $evidenceSummary
        }
        steps = @($Steps)
        artifacts = [ordered]@{
            log = $LogPath
            aggregateJunit = $AggregateJUnitPath
            acceptance = $AcceptancePath
            summary = $SummaryPath
        }
    }
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $SummaryPath -Encoding utf8

    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('# Enterprise Test Acceptance')
    $lines.Add('')
    $lines.Add("- Profile: $Profile")
    $lines.Add("- Git commit: $GitCommit")
    $lines.Add("- Worktree dirty (tracked or untracked): $WorktreeDirty")
    $lines.Add("- Started UTC: $($StartedUtc.ToString('o'))")
    $lines.Add("- Finished UTC: $($finishedUtc.ToString('o'))")
    $lines.Add("- Exit code: $DesiredExitCode")
    $lines.Add('- Exit codes: 0 accepted; 1 test/acceptance; 2 formal samples/local dependency blocked; 3 external environment; 4 runner/report/ragflow guard')
    $lines.Add("- Result: $(if ($DesiredExitCode -eq 0) { 'ACCEPTED' } else { 'NOT ACCEPTED' })")
    $lines.Add("- Live tests included: $($Profile -in @('Integration', 'All'))")
    $lines.Add("- Required Integration evidence: $requiredIntegrationEvidence ($requiredIntegrationEvidenceReason)")
    $lines.Add("- Offline implementation tests exist in repository; requested=$offlineImplementationTestsRequested; executed=$offlineImplementationTestsExecuted; status=$offlineImplementationTestStatus")
    $lines.Add('- Legacy v1/S3/demo regression is not counted as Required Integration evidence')
    $lines.Add('- Persistence backend: sqlite')
    $lines.Add('- PostgreSQL integration: N/A (no enterprise PostgreSQL runtime call path)')
    $lines.Add("- P1 status: $($summary.p1Status)")
    $lines.Add('')
    $lines.Add('| Step | Status | Exit | Detail |')
    $lines.Add('|---|---:|---:|---|')
    foreach ($step in $Steps) {
        $safeDetail = $step.detail.Replace('|', '\|').Replace("`r", ' ').Replace("`n", ' ')
        $lines.Add("| $($step.name) | $($step.status) | $($step.exitCode) | $safeDetail |")
    }
    Set-Content -LiteralPath $AcceptancePath -Value $lines -Encoding utf8
    Write-AggregateJUnit
}

try {
    Write-Log "enterprise acceptance profile=$Profile"
    $GitCommit = Get-GitCommit
    $TrackedBefore = @(Get-WorktreeState)
    $WorktreeDirty = $TrackedBefore.Count -gt 0
    Add-Step -Name git-state -Status passed -ExitCode 0 `
        -Detail "commit captured; trackedWorktreeDirty=$WorktreeDirty"
    $RagflowBefore = @(Get-RagflowState)
    Add-Step -Name ragflow-guard-before -Status passed -ExitCode 0 -Detail "captured $($RagflowBefore.Count) pre-existing path changes"

    $PythonRuntime = Resolve-Runtime -Explicit $PythonPath -RuntimeName 'Python' `
        -PathNames @('python', 'python3') `
        -EnvironmentNames @('TYRAG_BUNDLED_PYTHON', 'CODEX_BUNDLED_PYTHON') `
        -BundledCandidates {
            foreach ($root in Get-CodexResourceRoots) {
                Join-Path $root 'python\python.exe'
                Join-Path $root 'python\bin\python.exe'
                Join-Path $root 'cua_python\python.exe'
            }
    }
    Write-Log "Python runtime source=$($PythonRuntime.source)"

    if ($Profile -in @('Integration', 'All')) {
        Assert-NoIntegrationBypassTests
        Set-IntegrationAliases
        Invoke-IntegrationProbeStep
        Add-Step -Name live-environment -Status passed -ExitCode 0 `
            -Detail 'FILE_SHARE root, Asset Registry, RAGFlow, Redis/Valkey, Gateway, DB, and auth configuration is available'
    }

    $needsNode = $Profile -in @('P0', 'Integration', 'WP03', 'All')
    if ($needsNode) {
        $NodeRuntime = Resolve-Runtime -Explicit $NodePath -RuntimeName 'Node.js' `
            -PathNames @('node', 'node.exe') `
            -EnvironmentNames @('TYRAG_BUNDLED_NODE', 'CODEX_BUNDLED_NODE') `
            -BundledCandidates {
                foreach ($root in Get-CodexResourceRoots) {
                    Join-Path $root 'cua_node\bin\node.exe'
                    Join-Path $root 'node\node.exe'
                    Join-Path $root 'node\bin\node.exe'
                }
            }
        Write-Log "Node.js runtime source=$($NodeRuntime.source)"
    }

    Push-Location $RepoRoot
    $LocationPushed = $true
    [Environment]::SetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', '1', 'Process')
    [Environment]::SetEnvironmentVariable('TEMP', $RunTempDir, 'Process')
    [Environment]::SetEnvironmentVariable('TMP', $RunTempDir, 'Process')
    [Environment]::SetEnvironmentVariable('TMPDIR', $RunTempDir, 'Process')

    $importCheck = 'import aiosqlite, fastapi, httpx, jsonschema, jwt, pydantic, pytest, pytest_asyncio, yaml'
    $dependencyCode = Invoke-Logged -FilePath $PythonRuntime.path -Arguments @('-c', $importCheck) -Label 'python-dependencies'
    if ($dependencyCode -ne 0) {
        throw [DependencyException]::new('Python test dependencies are missing; use enterprise/requirements-test.txt explicitly')
    }
    Add-Step -Name python-dependencies -Status passed -ExitCode 0 -Detail 'required modules import successfully'

    if ($needsNode) {
        $tsc = Join-Path $WebRoot 'node_modules\typescript\bin\tsc'
        $vitest = Join-Path $WebRoot 'node_modules\vitest\vitest.mjs'
        if (-not (Test-Path -LiteralPath $tsc -PathType Leaf) -or -not (Test-Path -LiteralPath $vitest -PathType Leaf)) {
            throw [DependencyException]::new('enterprise/web node_modules is incomplete; the runner does not install packages')
        }
        $nodeCode = Invoke-Logged -FilePath $NodeRuntime.path -Arguments @('--version') -Label 'node-runtime'
        if ($nodeCode -ne 0) { throw [DependencyException]::new('Node.js runtime cannot be executed') }
        Add-Step -Name node-runtime -Status passed -ExitCode 0 -Detail "runtime source=$($NodeRuntime.source)"
    }

    if ($Profile -eq 'Contract') {
        Invoke-PytestStep -Name contract-static `
            -TestArguments @('enterprise/tests/test_v2_contract_static.py', '-q') `
            -JUnitName 'contract-static.xml'
    }

    if ($Profile -in @('P0', 'Integration', 'WP03', 'All')) {
        $offlineTests = @(
            'enterprise/tests', '-q',
            '--ignore=enterprise/tests/test_query_contract.py',
            '--ignore=enterprise/tests/test_wp03_contract.py',
            '--ignore=enterprise/tests/validate_mapping_strategies.py',
            '--ignore=enterprise/tests/test_v2_redis_integration.py',
            '--ignore=enterprise/tests/test_enterprise_runner.py'
        )
        Invoke-PytestStep -Name pytest-offline -TestArguments $offlineTests -JUnitName 'pytest-offline.xml'

        $tsc = Join-Path $WebRoot 'node_modules\typescript\bin\tsc'
        Invoke-CommandStep -Name tsc -FilePath $NodeRuntime.path -Arguments @(
            $tsc, '--noEmit', '--pretty', 'false', '-p', (Join-Path $WebRoot 'tsconfig.json')
        )
        Invoke-VitestStep
    }

    if ($Profile -in @('Integration', 'All')) {
        Invoke-RequiredIntegrationE2EStep
    }

    if ($Profile -eq 'WP03' -or $Profile -eq 'All') {
        if ($Profile -eq 'WP03') {
            Invoke-PytestStep -Name wp03-offline `
                -TestArguments @(
                    'enterprise/tests/test_wp03_evaluation.py',
                    'enterprise/tests/test_wp03_phase2.py',
                    'enterprise/tests/test_wp03_acceptance.py',
                    '-q'
                ) `
                -JUnitName 'wp03-offline.xml'
        }
        Invoke-WP03AcceptanceStep
    }
    if ($Profile -eq 'All') {
        Add-Step -Name p1-status -Status blocked -ExitCode 2 `
            -Detail 'P1 callback and attachment capabilities remain planned; FILE_SHARE v3 + formal v2 is the Required Integration evidence profile'
        Set-ExitCode 2
    }
} catch [DependencyException] {
    Write-Log "DEPENDENCY ERROR: $($_.Exception.Message)"
    Add-Step -Name dependency-check -Status failed -ExitCode 4 -Detail $_.Exception.Message
    Set-ExitCode 4
} catch [ExternalEnvironmentException] {
    Write-Log "EXTERNAL ENVIRONMENT ERROR: $($_.Exception.Message)"
    Add-Step -Name live-environment -Status blocked -ExitCode 3 -Detail $_.Exception.Message
    Set-ExitCode 3
} catch {
    Write-Log "RUNNER ERROR: $($_.Exception.Message)"
    Add-Step -Name runner -Status failed -ExitCode 4 -Detail $_.Exception.Message
    Set-ExitCode 4
} finally {
    if ($LocationPushed) { Pop-Location }
    try {
        $ragflowAfter = @(Get-RagflowState)
        $guardDiff = @(Compare-Object -ReferenceObject $RagflowBefore -DifferenceObject $ragflowAfter)
        if ($guardDiff.Count -gt 0) {
            $RagflowGuardUnchanged = $false
            Write-Log 'RAGFLOW GUARD VIOLATION: git status changed under ragflow/'
            Add-Step -Name ragflow-guard-after -Status failed -ExitCode 4 -Detail 'git status changed under ragflow/'
            Set-ExitCode 4
        } else {
            $RagflowGuardUnchanged = $true
            Add-Step -Name ragflow-guard-after -Status passed -ExitCode 0 -Detail 'git status under ragflow/ is unchanged'
        }
    } catch {
        $RagflowGuardUnchanged = $false
        Write-Log "RAGFLOW GUARD ERROR: $($_.Exception.Message)"
        Add-Step -Name ragflow-guard-after -Status failed -ExitCode 4 -Detail 'unable to verify ragflow git status'
        Set-ExitCode 4
    }
    try {
        Write-Reports
        Write-Log "summary=$SummaryPath exit=$DesiredExitCode"
    } catch {
        Set-ExitCode 4
        [Console]::Error.WriteLine("Unable to write acceptance reports: $($_.Exception.Message)")
    }
}

exit $DesiredExitCode
