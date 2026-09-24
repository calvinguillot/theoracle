@echo off
rem theoracle — opens the soundscape in its own Chrome window.
rem Start TouchDesigner first; the page reconnects by itself if it is not there yet.
rem
rem ?sink=  is part of the output device's name as Windows shows it. Change it to match.
rem The separate --user-data-dir matters: without it, if Chrome is already open,
rem these flags are silently ignored and the page may start muted.

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
set PROFILE=%~dp0runtime\chrome-synth

start "" %CHROME% ^
  --user-data-dir="%PROFILE%" ^
  --app="http://localhost:9980/?sink=CQ" ^
  --autoplay-policy=no-user-gesture-required ^
  --use-fake-ui-for-media-stream ^
  --disable-background-timer-throttling ^
  --disable-renderer-backgrounding ^
  --disable-backgrounding-occluded-windows ^
  --no-first-run
