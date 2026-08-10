[CmdletBinding()]
param(
    [ValidateSet('Contract', 'P0', 'Integration', 'WP03', 'All')]
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

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$WebRoot = Join-Path $RepoRoot 'enterprise\web'
$StartedUtc = [DateTime]::UtcNow
$RunStamp = $StartedUtc.ToString('yyyyMMddTHHmmssZ')
if ([string]::IsNullOrWhiteSpace($ArtifactRoot)) {
    $ArtifactRoot = Join-Path $RepoRoot 'artifacts\enterprise-tests'
}
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
$LocationPushed = $false

function Write-Log {
    param([string]$Message)
    $line = "{0} {1}" -f [DateTime]::UtcNow.ToString('o'), $Message
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
        detail = $Detail
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
        $line = $_.ToString()
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
    $code = Invoke-Logged -FilePath $PythonRuntime.path -Arguments $arguments -Label $Name
    $counts = Read-JUnitCounts -Path $junit
    if ($null -eq $counts) {
        Add-Step -Name $Name -Status failed -ExitCode $code -Detail 'JUnit report was not produced' -JUnit $junit
        Set-ExitCode 1
        return
    }
    $detail = "tests=$($counts.tests) failures=$($counts.failures) errors=$($counts.errors) skipped=$($counts.skipped)"
    if ($code -ne 0 -or $counts.tests -eq 0 -or $counts.failures -gt 0 -or $counts.errors -gt 0 -or $counts.skipped -gt 0) {
        Add-Step -Name $Name -Status failed -ExitCode $code -Detail $detail -JUnit $junit
        Set-ExitCode 1
    } else {
        Add-Step -Name $Name -Status passed -ExitCode 0 -Detail $detail -JUnit $junit
    }
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
        Add-Step -Name vitest -Status failed -ExitCode $code -Detail 'JUnit report was not produced' -JUnit $junit
        Set-ExitCode 1
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
    $summary = [ordered]@{
        profile = $Profile
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
        liveTestsIncluded = ($Profile -in @('Integration', 'WP03', 'All'))
        persistence = [ordered]@{
            backend = 'sqlite'
            postgresIntegration = 'not_applicable'
            reason = 'no enterprise PostgreSQL runtime call path'
        }
        p1TestsRequested = ($Profile -eq 'All')
        p1ExpectedTests = @(
            'enterprise/tests/test_v2_callback_contract.py',
            'enterprise/tests/test_v2_attachment_contract.py'
        )
        steps = @($Steps)
        artifacts = [ordered]@{
            log = $LogPath
            aggregateJunit = $AggregateJUnitPath
            acceptance = $AcceptancePath
        }
    }
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $SummaryPath -Encoding utf8

    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('# Enterprise Test Acceptance')
    $lines.Add('')
    $lines.Add("- Profile: $Profile")
    $lines.Add("- Started UTC: $($StartedUtc.ToString('o'))")
    $lines.Add("- Finished UTC: $($finishedUtc.ToString('o'))")
    $lines.Add("- Exit code: $DesiredExitCode")
    $lines.Add('- Exit codes: 0 accepted; 1 test/acceptance; 2 formal samples/local dependency blocked; 3 external environment; 4 runner/report/ragflow guard')
    $lines.Add("- Result: $(if ($DesiredExitCode -eq 0) { 'ACCEPTED' } else { 'NOT ACCEPTED' })")
    $lines.Add("- Live tests included: $($Profile -in @('Integration', 'WP03', 'All'))")
    $lines.Add('- Persistence backend: sqlite')
    $lines.Add('- PostgreSQL integration: N/A (no enterprise PostgreSQL runtime call path)')
    $lines.Add("- P1 callback/attachment tests requested: $($Profile -eq 'All')")
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

    $needsNode = $Profile -in @('P0', 'Integration', 'All')
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

    if ($Profile -in @('Integration', 'All')) {
        $liveBase = [Environment]::GetEnvironmentVariable('ENTERPRISE_RAGFLOW_BASE_URL')
        $assetRegistryBase = [Environment]::GetEnvironmentVariable('ENTERPRISE_ASSET_REGISTRY_BASE_URL')
        $redisUrl = [Environment]::GetEnvironmentVariable('ENTERPRISE_REDIS_URL')
        $liveKeyPresent = -not [string]::IsNullOrWhiteSpace(
            [Environment]::GetEnvironmentVariable('ENTERPRISE_RAGFLOW_API_KEY')
        )
        $liveUri = $null
        $liveBaseValid = -not [string]::IsNullOrWhiteSpace($liveBase) -and
            [Uri]::TryCreate($liveBase, [UriKind]::Absolute, [ref]$liveUri) -and
            $liveUri.Scheme -in @('http', 'https')
        $assetRegistryUri = $null
        $assetRegistryValid = -not [string]::IsNullOrWhiteSpace($assetRegistryBase) -and
            [Uri]::TryCreate($assetRegistryBase, [UriKind]::Absolute, [ref]$assetRegistryUri) -and
            $assetRegistryUri.Scheme -in @('http', 'https')
        $redisUri = $null
        $redisValid = -not [string]::IsNullOrWhiteSpace($redisUrl) -and
            [Uri]::TryCreate($redisUrl, [UriKind]::Absolute, [ref]$redisUri) -and
            $redisUri.Scheme -in @('redis', 'rediss')
        if (-not $liveBaseValid -or -not $liveKeyPresent -or -not $assetRegistryValid -or -not $redisValid) {
            throw [ExternalEnvironmentException]::new(
                'Integration requires valid ENTERPRISE_RAGFLOW_BASE_URL/API_KEY, ENTERPRISE_ASSET_REGISTRY_BASE_URL, and ENTERPRISE_REDIS_URL'
            )
        }
        Add-Step -Name live-environment -Status passed -ExitCode 0 -Detail 'RAGFlow, Asset Registry, and Redis configuration is present'
    }

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

    if ($Profile -in @('P0', 'Integration', 'All')) {
        $offlineTests = @(
            'enterprise/tests', '-q',
            '--ignore=enterprise/tests/test_query_contract.py',
            '--ignore=enterprise/tests/test_wp03_contract.py',
            '--ignore=enterprise/tests/validate_mapping_strategies.py',
            '--ignore=enterprise/tests/test_v2_redis_integration.py'
        )
        Invoke-PytestStep -Name pytest-offline -TestArguments $offlineTests -JUnitName 'pytest-offline.xml'

        $tsc = Join-Path $WebRoot 'node_modules\typescript\bin\tsc'
        Invoke-CommandStep -Name tsc -FilePath $NodeRuntime.path -Arguments @(
            $tsc, '--noEmit', '--pretty', 'false', '-p', (Join-Path $WebRoot 'tsconfig.json')
        )
        Invoke-VitestStep
    }

    if ($Profile -in @('Integration', 'All')) {
        $liveTests = @(
            'enterprise/tests/test_query_contract.py',
            'enterprise/tests/test_wp03_contract.py',
            'enterprise/tests/validate_mapping_strategies.py',
            'enterprise/tests/test_v2_redis_integration.py',
            '-q'
        )
        Invoke-PytestStep -Name pytest-live-integration -TestArguments $liveTests -JUnitName 'pytest-live.xml'
    }

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

    if ($Profile -in @('WP03', 'All')) {
        Invoke-WP03AcceptanceStep
    }
    if ($Profile -eq 'All') {
        $p1Tests = @(
            'enterprise/tests/test_v2_callback_contract.py',
            'enterprise/tests/test_v2_attachment_contract.py'
        )
        $missingP1 = @($p1Tests | Where-Object { -not (Test-Path -LiteralPath (Join-Path $RepoRoot $_) -PathType Leaf) })
        if ($missingP1.Count -gt 0) {
            Add-Step -Name pytest-p1-callback-attachment -Status failed -ExitCode 1 `
                -Detail "required P1 test files are missing: $($missingP1 -join ', ')"
            Set-ExitCode 1
        } else {
            Invoke-PytestStep -Name pytest-p1-callback-attachment `
                -TestArguments @($p1Tests + '-q') -JUnitName 'pytest-p1.xml'
        }
    }
} catch [DependencyException] {
    Write-Log "DEPENDENCY ERROR: $($_.Exception.Message)"
    Add-Step -Name dependency-check -Status blocked -ExitCode 2 -Detail $_.Exception.Message
    Set-ExitCode 2
} catch [ExternalEnvironmentException] {
    Write-Log "EXTERNAL ENVIRONMENT ERROR: $($_.Exception.Message)"
    Add-Step -Name live-environment -Status blocked -ExitCode 3 -Detail $_.Exception.Message
    Set-ExitCode 3
} catch {
    Write-Log "RUNNER ERROR: $($_.Exception.Message)"
    Add-Step -Name runner -Status blocked -ExitCode 4 -Detail $_.Exception.Message
    Set-ExitCode 4
} finally {
    if ($LocationPushed) { Pop-Location }
    try {
        $ragflowAfter = @(Get-RagflowState)
        $guardDiff = @(Compare-Object -ReferenceObject $RagflowBefore -DifferenceObject $ragflowAfter)
        if ($guardDiff.Count -gt 0) {
            Write-Log 'RAGFLOW GUARD VIOLATION: git status changed under ragflow/'
            Add-Step -Name ragflow-guard-after -Status failed -ExitCode 4 -Detail 'git status changed under ragflow/'
            Set-ExitCode 4
        } else {
            Add-Step -Name ragflow-guard-after -Status passed -ExitCode 0 -Detail 'git status under ragflow/ is unchanged'
        }
    } catch {
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
