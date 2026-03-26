@echo off
cd /d C:\Users\bgopu\servicenow-rpt
call .venv\Scripts\activate.bat
python alert_new_incidents.py >> logs\alert_run.log 2>&1
