param([string]$Action='demo',[string]$Python='python')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path $PSScriptRoot -Parent
if(Test-Path "$projectRoot\.env") {
 foreach($line in Get-Content -LiteralPath "$projectRoot\.env") {
  if($line -match '^([A-Z_]+)=(.*)$') {[Environment]::SetEnvironmentVariable($Matches[1],$Matches[2],'Process')}
 }
}
if(Test-Path "$projectRoot\.local\pydeps") {$env:PYTHONPATH="$projectRoot\.local\pydeps;$env:PYTHONPATH"}
$commands=@{migrate=@('scripts/migrate.py');demo=@('scripts/demo.py');seed=@('ingest/p1_simulate_plant.py');test=@('-m','pytest','-q','tests');download=@('scripts/download_data.py');export=@('scripts/export_results.py');timescale=@('scripts/timescale.py');replay=@('scripts/replay_demo.py')}
if(-not $commands.ContainsKey($Action)){throw "Unknown action: $Action"}
Push-Location $projectRoot
try {& $Python @($commands[$Action]);if($LASTEXITCODE -ne 0){throw "Failed: $Action"}} finally {Pop-Location}
