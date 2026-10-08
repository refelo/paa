[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Project,
    [ValidateSet('install','status','recover','uninstall','rollback')][string]$Action = 'install',
    [ValidatePattern('^v[0-9]+\.[0-9]+\.[0-9]+$')][string]$Version = 'v0.0.1',
    [ValidateSet('enable','disable')][string]$Aesthetics,
    [string]$Library,
    [switch]$AllowPreview,
    [switch]$NonInteractive,
    [switch]$ManagedPython,
    [string]$PackagePath,
    [string]$Wheelhouse
)
$ErrorActionPreference = 'Stop'
$projectPath = [IO.Path]::GetFullPath($Project)
$userProfilePath = [Environment]::GetFolderPath('UserProfile').TrimEnd('\')
if ($projectPath.TrimEnd('\') -eq $userProfilePath -or
    $projectPath.TrimEnd('\') -eq [IO.Path]::GetPathRoot($projectPath).TrimEnd('\')) {
    throw 'Select a project directory, not your home directory or a drive root.'
}
foreach ($globalDirectory in @('.codex','.agents')) {
    $globalPath = Join-Path $userProfilePath $globalDirectory
    if ($projectPath.TrimEnd('\') -eq $globalPath -or
        $projectPath.StartsWith($globalPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Global Codex/Agent directories are not project installation targets.'
    }
}
$ancestor = $projectPath
while ($ancestor) {
    if ([IO.Path]::GetExtension($ancestor.TrimEnd('\')) -eq '.library') {
        throw 'A project cannot be installed inside an Eagle library.'
    }
    if ((Test-Path -LiteralPath $ancestor) -and
        ((Get-Item -Force -LiteralPath $ancestor).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Installation targets cannot traverse symbolic links or junctions.'
    }
    $parent = [IO.Directory]::GetParent($ancestor)
    $ancestor = if ($parent) { $parent.FullName } else { $null }
}
if ($Library) {
    $libraryPath = [IO.Path]::GetFullPath($Library)
    if ([IO.Path]::GetExtension($libraryPath.TrimEnd('\')) -ne '.library' -or
        -not (Test-Path -LiteralPath (Join-Path $libraryPath 'images') -PathType Container)) {
        throw 'Explicit Eagle library is not readable.'
    }
    if ($projectPath.TrimEnd('\') -eq $libraryPath.TrimEnd('\') -or
        $projectPath.StartsWith($libraryPath.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installation cannot write inside its source library.'
    }
}
New-Item -ItemType Directory -Path $projectPath -Force | Out-Null
$receiptPath = Join-Path $projectPath '.local/install/receipt.json'
$retainedInstaller = Join-Path $projectPath ".local/install/install-$Version.ps1"

if ($Action -ne 'install') {
    if (-not (Test-Path -LiteralPath $receiptPath)) {
        if ($Action -in @('status','uninstall')) { '{"installed":false}'; return }
        throw 'No managed PAA installation exists in this project.'
    }
    $receipt = Get-Content -Raw -LiteralPath $receiptPath | ConvertFrom-Json
    $runtimePython = [IO.Path]::GetFullPath($receipt.runtime.python)
    if (-not $runtimePython.StartsWith($projectPath.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Runtime belongs to a different project.'
    }
    & $runtimePython -I -X utf8 -m paa.installation $Action --target $projectPath
    if ($LASTEXITCODE -ne 0) { throw 'PAA management operation failed; data retained.' }
    return
}
if ((Test-Path -LiteralPath $retainedInstaller) -and
    (Get-FileHash -LiteralPath $retainedInstaller).Hash -ne (Get-FileHash -LiteralPath $PSCommandPath).Hash) {
    throw 'Retained installer was changed; preserve it and choose a new version or project.'
}

$downloadRoot = Join-Path ([IO.Path]::GetTempPath()) ('paa-package-' + [Guid]::NewGuid().ToString('N'))
if (-not $PackagePath) {
    New-Item -ItemType Directory -Path $downloadRoot | Out-Null
    $baseUrl = "https://github.com/refelo/paa/releases/download/$Version"
    $archive = Join-Path $downloadRoot 'package.zip'
    $checksum = Join-Path $downloadRoot 'package.sha256'
    Invoke-WebRequest "$baseUrl/paa-$Version-windows.zip" -OutFile $archive
    Invoke-WebRequest "$baseUrl/paa-$Version-windows.zip.sha256" -OutFile $checksum
    $expected = ((Get-Content -Raw -LiteralPath $checksum).Trim() -split '\s+')[0]
    if ($expected -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
        throw 'Downloaded release checksum mismatch.'
    }
    $PackagePath = Join-Path $downloadRoot 'source'
    Expand-Archive -LiteralPath $archive -DestinationPath $PackagePath
}
$sourcePath = [IO.Path]::GetFullPath($PackagePath)
$manifest = Get-Content -Raw -LiteralPath (Join-Path $sourcePath 'RELEASE_MANIFEST.json') | ConvertFrom-Json
if ($manifest.version -ne $Version.Substring(1)) { throw 'Package version mismatch.' }
foreach ($property in $manifest.files.PSObject.Properties) {
    $filePath = [IO.Path]::GetFullPath((Join-Path $sourcePath $property.Name))
    if (-not $filePath.StartsWith($sourcePath.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Package path escaped its directory.'
    }
    if ((Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash -ne $property.Value) {
        throw "Package file checksum mismatch: $($property.Name)"
    }
}

$pythonPath = $null
foreach ($candidate in $(if ($ManagedPython) { @() } else { @(Get-Command python,python3 -ErrorAction SilentlyContinue) })) {
    if ($candidate.Source -match '\\Microsoft\\WindowsApps\\') { continue }
    $found = & $candidate.Source -I -c 'import sys; sys.exit(1) if sys.version_info[:2] != (3,14) else print(sys.executable)' 2>$null
    if ($LASTEXITCODE -eq 0 -and $found) { $pythonPath = [string]$found; break }
}
if (-not $pythonPath -and -not $ManagedPython -and (Get-Command py -ErrorAction SilentlyContinue)) {
    $found = & py -3.14 -I -c 'import sys; print(sys.executable)' 2>$null
    if ($LASTEXITCODE -eq 0 -and $found) { $pythonPath = [string]$found }
}
if (-not $pythonPath) {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCommand) { $uvPath = $uvCommand.Source }
    else {
        $toolRoot = Join-Path $projectPath '.local/install/tools/uv-0.12.1'
        New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
        $uvZip = Join-Path $toolRoot 'uv.zip'
        Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/download/0.12.1/uv-x86_64-pc-windows-msvc.zip' -OutFile $uvZip
        if ((Get-FileHash -LiteralPath $uvZip -Algorithm SHA256).Hash -ne '8fcb0cb46e1229065e344758980924e569bef5882ef45f46fada8fb24e06b74a') {
            throw 'uv checksum mismatch.'
        }
        Expand-Archive -LiteralPath $uvZip -DestinationPath $toolRoot -Force
        $uvBinaries = @(Get-ChildItem -LiteralPath $toolRoot -Filter uv.exe -Recurse -File)
        if ($uvBinaries.Count -ne 1) { throw 'Expected one uv executable.' }
        $uvPath = $uvBinaries[0].FullName
    }
    $priorPythonDir = $env:UV_PYTHON_INSTALL_DIR
    $priorCacheDir = $env:UV_CACHE_DIR
    try {
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $projectPath '.local/install/python'
        $env:UV_CACHE_DIR = Join-Path $projectPath '.local/install/uv-cache'
        & $uvPath python install 3.14 --no-config --no-bin --no-registry
        if ($LASTEXITCODE -ne 0) { throw 'Project Python download failed.' }
        $pythonPath = [string](& $uvPath python find 3.14 --managed-python --no-project --no-config)
        if ($LASTEXITCODE -ne 0) { throw 'Project Python was not found.' }
    }
    finally { $env:UV_PYTHON_INSTALL_DIR = $priorPythonDir; $env:UV_CACHE_DIR = $priorCacheDir }
}
if (-not $Aesthetics -and -not $NonInteractive -and -not (Test-Path -LiteralPath $receiptPath)) {
    $answer = Read-Host 'Enable optional author aesthetics? y=yes, n=no, Enter=unanswered (v0.0.1 bundles no third-party preferences)'
    if ($answer -match '^(y|yes)$') { $Aesthetics = 'enable' }
    elseif ($answer -match '^(n|no)$') { $Aesthetics = 'disable' }
    elseif ($answer) { throw 'Unrecognized choice; no preference was recorded.' }
}
$installArguments = @('-X','utf8',(Join-Path $sourcePath 'scripts/manage_install.py'),'install','--target',$projectPath)
if ($Aesthetics) { $installArguments += @('--aesthetics',$Aesthetics) }
if ($Library) { $installArguments += @('--library',[IO.Path]::GetFullPath($Library)) }
if ($AllowPreview) { $installArguments += '--allow-preview' }
if ($Wheelhouse) { $installArguments += @('--wheelhouse',[IO.Path]::GetFullPath($Wheelhouse)) }
& $pythonPath @installArguments
if ($LASTEXITCODE -ne 0) { throw 'Installation failed; use recover for a pending transaction.' }
if (-not (Test-Path -LiteralPath $retainedInstaller)) { Copy-Item -LiteralPath $PSCommandPath -Destination $retainedInstaller }
Write-Output "Installed in $projectPath. Management: & '$retainedInstaller' -Project '$projectPath' -Action status"
