@echo off
rem Runs the data logger forever and restarts it after crashes.
rem Output goes to data_store\logger.log
cd /d %~dp0
if not exist data_store mkdir data_store
:loop
call .venv\Scripts\python.exe -m weatherbot.data.logger >> data_store\logger.log 2>&1
timeout /t 10
goto loop
