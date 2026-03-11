@echo off
title ServiceNow Report - Full Pipeline
echo.
echo ============================================================
echo   ServiceNow Incident Report - Full Pipeline
echo ============================================================
echo.
echo  1. Download from ServiceNow + Generate Report
echo  2. Download + Generate + Upload to SharePoint
echo  3. Upload latest report to SharePoint only
echo  4. Generate report from existing CSV only
echo.
set /p choice="Choose option (1-4): "

call .venv\Scripts\activate.bat

if "%choice%"=="1" (
    python servicenow_downloader.py
) else if "%choice%"=="2" (
    python servicenow_downloader.py --upload-to-sharepoint
) else if "%choice%"=="3" (
    python sharepoint_uploader.py
) else if "%choice%"=="4" (
    python main.py --input reports\Incidents_list.csv
    for /f %%i in ('dir /b /od reports\ServicenowReport_WW*.html') do set LATEST=%%i
    start reports\%LATEST%
) else (
    echo Invalid choice.
)

echo.
pause
