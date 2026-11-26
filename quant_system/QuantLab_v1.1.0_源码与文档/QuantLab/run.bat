@echo off
REM QuantLab launcher: use the Python 3.12 install that has akshare/Flask.
REM The bare "python" on this machine points to a different interpreter.
set "PY=C:\Users\G2738\AppData\Local\Programs\Python\Python312\python.exe"
cd /d "%~dp0"
echo Starting QuantLab ... http://127.0.0.1:5000
"%PY%" app.py
pause
