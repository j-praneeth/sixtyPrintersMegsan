@echo off
REM LIMS Print Hub - status the service (self-elevates via install-hub-service.bat).
call "%~dp0install-hub-service.bat" status
