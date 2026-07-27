@echo off
REM ===================================================================
REM  Decode (decrypt) an AES-256 encrypted PDF from this toolkit.
REM  Double-click and pick file(s), or drag PDFs onto this .bat.
REM  Prompts for the password (or set VCP_PDF_PASSWORD). No admin needed.
REM  Output: <name>-decrypted.pdf next to each input.
REM ===================================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0decode-pdf.ps1" -Files %*
