param([ValidateSet('2025','2026','2027')][string]$MayaVersion)
$ErrorActionPreference = 'Stop'
$farmRoot = $PSScriptRoot
$versionsToTry = if ($MayaVersion) { @($MayaVersion) } else { @('2027','2026','2025') }
$mayaPython = $null
foreach ($version in $versionsToTry) {
    $candidate = Join-Path $env:ProgramFiles "Autodesk\Maya$version\bin\mayapy.exe"
    if (Test-Path -LiteralPath $candidate) { $mayaPython = $candidate; break }
    $workerFolder = Join-Path $farmRoot 'workers'
    if (Test-Path -LiteralPath $workerFolder) {
        foreach ($folder in Get-ChildItem -LiteralPath $workerFolder -Directory) {
            if (-not $folder.Name.StartsWith($env:COMPUTERNAME + '_', [System.StringComparison]::OrdinalIgnoreCase)) { continue }
            $configPath = Join-Path $folder.FullName 'config.json'
            if (-not (Test-Path -LiteralPath $configPath)) { continue }
            $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $customPath = $config.maya_paths.$version
            if ($customPath) {
                $candidate = Join-Path $customPath 'bin\mayapy.exe'
                if (Test-Path -LiteralPath $candidate) { $mayaPython = $candidate; break }
            }
        }
    }
    if ($mayaPython) { break }
}
if (-not $mayaPython) {
    Write-Host 'Maya 2025, 2026 or 2027 was not found. Run mayapy.exe farm_cli.py gui from your Maya installation.'
    Read-Host 'Press Enter'
    exit 1
}
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PORTABLE_FARM_LAUNCH_ROOT = $farmRoot
& $mayaPython -B -c "import os,sys,runpy; r=os.environ['PORTABLE_FARM_LAUNCH_ROOT']; p=os.path.join(r,'farm_cli.py'); sys.path.insert(0,r); sys.argv=[p,'--root',r,'gui']; runpy.run_path(p,run_name='__main__')"
if ($LASTEXITCODE -ne 0) { Read-Host 'Startup failed. Press Enter' }
