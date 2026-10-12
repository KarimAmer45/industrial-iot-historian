param([ValidateSet('start','stop','status')][string]$Action='status')
$projectRoot=Split-Path $PSScriptRoot -Parent
$pgCtl=Join-Path $projectRoot '.local\postgres\pgsql\bin\pg_ctl.exe'
$pgData=Join-Path $projectRoot '.local\pgdata'
switch($Action){
 start { & $pgCtl -D $pgData -l "$projectRoot\.local\postgres.log" -o '-h 127.0.0.1 -p 55435' -w start }
 stop { & $pgCtl -D $pgData -m fast -w stop }
 status { & $pgCtl -D $pgData status }
}
exit $LASTEXITCODE
