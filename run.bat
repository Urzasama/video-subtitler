@echo off
setlocal
set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe
"%PY%" subtitler.py %*
