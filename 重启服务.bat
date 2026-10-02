@echo off
setlocal
cd /d "%~dp0"
set "WECHAT_LAUNCHER="
for %%F in ("%~dp0*.bat") do (
  findstr /I /X /C:"python server.py" "%%~fF" >nul
  if not errorlevel 1 if not defined WECHAT_LAUNCHER set "WECHAT_LAUNCHER=%%~fF"
)
if not defined WECHAT_LAUNCHER (
  echo Could not find the existing client launcher. Nothing was changed.
  pause
  exit /b 1
)
set "WECHAT_ROOT=%~dp0"
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; try { $processes=@(Get-CimInstance Win32_Process); $targetIds=@(); $listeners=@(Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 8765 -ErrorAction SilentlyContinue); if ($listeners.Count -gt 0) { $state=Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/state' -TimeoutSec 3; $required=@('groups','tasks','status','running','ready','key_configured','my_names'); foreach($name in $required){if($state.PSObject.Properties.Name -notcontains $name){throw 'Port 8765 does not look like Wechat_detector. No process was stopped.'}}; foreach($listener in $listeners){$p=$processes|Where-Object ProcessId -eq $listener.OwningProcess; if(-not $p -or $p.Name -notmatch '^pythonw?\.exe$' -or $p.CommandLine -notmatch '(?i)\bserver\.py\b'){throw 'Port 8765 is held by an unexpected process. No process was stopped.'}; if($targetIds -notcontains [int]$p.ProcessId){$targetIds += [int]$p.ProcessId}} }; $launcher=$env:WECHAT_LAUNCHER; $launchers=@($processes|Where-Object { $_.Name -ieq 'cmd.exe' -and $_.CommandLine -like ('*' + $launcher + '*') }|ForEach-Object {[int]$_.ProcessId}); foreach($p in $processes){if($p.Name -match '^pythonw?\.exe$' -and $p.CommandLine -match '(?i)\bserver\.py\b' -and $launchers -contains [int]$p.ParentProcessId){if($targetIds -notcontains [int]$p.ProcessId){$targetIds += [int]$p.ProcessId}}}; foreach($id in $targetIds){Stop-Process -Id $id -ErrorAction Stop; Write-Host ('Stopped Wechat_detector server PID ' + $id)}; for($i=0;$i -lt 10;$i++){ $remaining=@(Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 8765 -ErrorAction SilentlyContinue); if($remaining.Count -eq 0){break}; Start-Sleep -Seconds 1 }; if(@(Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 8765 -ErrorAction SilentlyContinue).Count -gt 0){throw 'Port 8765 is still busy. The new server was not started.'}; Write-Host 'Starting Wechat_detector...' } catch { Write-Error $_; exit 2 }"
if errorlevel 1 (
  echo.
  echo Restart stopped. Review the message above; no new server was launched.
  pause
  exit /b 1
)
start "" "%WECHAT_LAUNCHER%"
