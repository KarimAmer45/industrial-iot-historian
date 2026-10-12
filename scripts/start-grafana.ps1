$ErrorActionPreference='Stop'
$projectRoot=Split-Path $PSScriptRoot -Parent
$settings=Get-Content -Raw -LiteralPath "$projectRoot\.local\grafana-env.json" | ConvertFrom-Json
foreach($p in $settings.PSObject.Properties){[Environment]::SetEnvironmentVariable($p.Name,$p.Value,'Process')}
$grafanaRoot=Join-Path $projectRoot '.local\grafana\grafana-13.2.3'
$process=Start-Process -FilePath "$grafanaRoot\bin\grafana.exe" -ArgumentList 'server' -WorkingDirectory $grafanaRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput "$projectRoot\.local\grafana-stdout.log" -RedirectStandardError "$projectRoot\.local\grafana-stderr.log"
$process.Id | Set-Content -LiteralPath "$projectRoot\.local\grafana.pid"
Write-Output "Grafana PID $($process.Id); http://127.0.0.1:3003/d/partec-line-historian"
