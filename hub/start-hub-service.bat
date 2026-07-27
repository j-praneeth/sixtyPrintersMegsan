@echo off
REM LIMS Print Hub - start the service (self-elevates via install-hub-service.bat).
call "%~dp0install-hub-service.bat" start
