[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [string]$RagflowImage = 'tyrag/ragflow:v0.26.4',
    [string]$GatewayImage = 'tyrag/enterprise-gateway:v0.26.4',
    [string]$WebImage = 'tyrag/enterprise-web:v0.26.4',
    [switch]$IncludeDiagnostics,
    [switch]$CreateZip
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $RepoRoot 'artifacts\offline-release'
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

$GitStatus = @(git -C $RepoRoot status --porcelain --untracked-files=all)
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to read Git status'
}
if ($GitStatus.Count -gt 0) {
    throw 'Release packaging requires a clean Git worktree'
}
$SourceCommit = (& git -C $RepoRoot rev-parse --verify HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SourceCommit)) {
    throw 'Unable to resolve the release source commit'
}
$PatchManifestPath = Join-Path $RepoRoot 'patches\manifest.yaml'
$PatchManifestText = Get-Content -LiteralPath $PatchManifestPath
$UpstreamTag = (($PatchManifestText | Select-String '^\s+upstream_tag:\s*(\S+)\s*$' | Select-Object -First 1).Matches.Groups[1].Value).Trim('"''')
$UpstreamCommit = (($PatchManifestText | Select-String '^\s+upstream_commit:\s*(\S+)\s*$' | Select-Object -First 1).Matches.Groups[1].Value).Trim('"''')
if ([string]::IsNullOrWhiteSpace($UpstreamTag) -or [string]::IsNullOrWhiteSpace($UpstreamCommit)) {
    throw 'patches/manifest.yaml must declare upstream_tag and upstream_commit'
}

$Images = [System.Collections.Generic.List[string]]::new()
$Images.Add($RagflowImage)
$Images.Add($GatewayImage)
$Images.Add('mysql:8.0.40')
$Images.Add('elasticsearch:8.11.3')
$Images.Add('pgsty/minio:RELEASE.2026-03-25T00-00-00Z')
$Images.Add('valkey/valkey:8')
if ($IncludeDiagnostics) {
    $Images.Add($WebImage)
}

if (Test-Path -LiteralPath $OutputDirectory) {
    throw "Output directory already exists: $OutputDirectory"
}

$ImageDirectory = Join-Path $OutputDirectory 'images'
$FileDirectory = Join-Path $OutputDirectory 'files'
New-Item -ItemType Directory -Path $ImageDirectory, $FileDirectory -Force | Out-Null

foreach ($image in $Images) {
    docker image inspect $image *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Required local image is missing: $image"
    }
}

$ProductionRoot = Join-Path $RepoRoot 'deploy\production'
Copy-Item (Join-Path $ProductionRoot 'docker-compose.yml') (Join-Path $OutputDirectory 'docker-compose.yml')
Copy-Item (Join-Path $ProductionRoot 'production.env.example') (Join-Path $OutputDirectory 'production.env.example')
Copy-Item (Join-Path $ProductionRoot 'install-offline.sh') (Join-Path $OutputDirectory 'install-offline.sh')
Copy-Item (Join-Path $ProductionRoot 'README.md') (Join-Path $OutputDirectory 'README.md')
Copy-Item (Join-Path $ProductionRoot 'docker-compose.test.yml') (Join-Path $OutputDirectory 'docker-compose.test.yml')
$upstreamInitPath = Join-Path $RepoRoot 'ragflow\docker\init.sql'
$releaseInitPath = Join-Path $ProductionRoot 'files\init.sql'
$upstreamInitText = (Get-Content -LiteralPath $upstreamInitPath -Raw).Replace("`r`n", "`n").Trim()
$releaseInitText = (Get-Content -LiteralPath $releaseInitPath -Raw).Replace("`r`n", "`n").Trim()
if ($upstreamInitText -ne $releaseInitText) {
    throw 'Production init.sql is not identical to ragflow/docker/init.sql'
}
Copy-Item $upstreamInitPath (Join-Path $FileDirectory 'init.sql')

$ImageArchive = Join-Path $ImageDirectory 'tyrag-images.tar'
& docker save --output $ImageArchive @Images
if ($LASTEXITCODE -ne 0) {
    throw 'docker save failed'
}

$ImageManifest = foreach ($image in $Images) {
    $inspect = @(docker image inspect $image | ConvertFrom-Json)[0]
    [ordered]@{
        image = $image
        id = $inspect.Id
        repoTags = @($inspect.RepoTags)
        repoDigests = @($inspect.RepoDigests)
        architecture = $inspect.Architecture
        os = $inspect.Os
        size = $inspect.Size
    }
}

$Manifest = [ordered]@{
    release = 'TYRAG production pilot v0.26.4'
    generatedAtUtc = [DateTime]::UtcNow.ToString('o')
    sourceCommit = $SourceCommit
    upstreamTag = $UpstreamTag
    upstreamCommit = $UpstreamCommit
    composeSha256 = (Get-FileHash -LiteralPath (Join-Path $OutputDirectory 'docker-compose.yml') -Algorithm SHA256).Hash.ToLowerInvariant()
    patchManifestSha256 = (Get-FileHash -LiteralPath $PatchManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    images = @($ImageManifest)
    diagnosticsIncluded = [bool]$IncludeDiagnostics
    compose = 'docker-compose.yml'
    testCompose = 'docker-compose.test.yml'
    envTemplate = 'production.env.example'
    dataPolicy = 'Docker volumes and production .env are not included; back them up separately.'
}
$Manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'image-manifest.json') -Encoding utf8

$HashLines = Get-ChildItem -LiteralPath $OutputDirectory -File -Recurse |
    Where-Object { $_.Name -ne 'SHA256SUMS' } |
    ForEach-Object {
        $relative = $_.FullName.Substring($OutputDirectory.Length + 1).Replace('\', '/')
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $relative"
    }
Set-Content -LiteralPath (Join-Path $OutputDirectory 'SHA256SUMS') -Value $HashLines -Encoding ascii

if ($CreateZip) {
    Compress-Archive -Path (Join-Path $OutputDirectory '*') -DestinationPath "$OutputDirectory.zip" -CompressionLevel Optimal
}

Write-Output "Offline release created: $OutputDirectory"
Write-Output "Images: $($Images.Count)"
