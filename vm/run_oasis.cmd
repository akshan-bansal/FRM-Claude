@echo off
setlocal EnableExtensions
rem ---------------------------------------------------------------------------------------------
rem  One small OASIS run. Double-click this file.
rem   - Hard spend cap is in the seed: $1, 5 agents, 2 steps, Haiku 4.5.
rem   - Spends NOTHING until you type YES. Your key is typed into this window and kept only here.
rem   - First use copies the finished runner from your own saved edit history to
rem     vm\oasis_runner_live.py (the runner in git is an older, unfinished stub).
rem   - Results land in state\oasis_inbox. If nothing happens in the simulation, the reason is in
rem     state\oasis_inbox\log (OASIS logs LLM errors there instead of raising them).
rem ---------------------------------------------------------------------------------------------
set "REPO=%~dp0.."
set "RUNNER=%~dp0oasis_runner_live.py"
set "SEED=%REPO%\state\oasis_seeds\fc6ca01860ab.json"
set "WORK=%REPO%\state\oasis_inbox"
set "SAVED=%USERPROFILE%\.claude\file-history\d83b76e4-dc7f-4dd3-a5ca-39751196a656\1435c6d0055dee84@v4"
set "PY=C:\Users\PC\oasis-env\Scripts\python.exe"

if not exist "%PY%" goto nopy
if not exist "%SEED%" goto noseed
if exist "%RUNNER%" goto haverunner
if not exist "%SAVED%" goto nosaved
echo Copying the saved runner to %RUNNER%
copy /Y "%SAVED%" "%RUNNER%" >nul
:haverunner
if not exist "%WORK%" mkdir "%WORK%"
if not exist "%WORK%\log" mkdir "%WORK%\log"
cd /d "%WORK%"

echo.
echo === Dry run: prints the budget and spends nothing ===
"%PY%" "%RUNNER%" "%SEED%" --out "%WORK%\dry.json"
echo.
set "GO="
set /p "GO=Spend up to $1 on a real run? Type YES to continue: "
if /I not "%GO%"=="YES" goto done

set "ANTHROPIC_API_KEY="
set /p "ANTHROPIC_API_KEY=Paste your Anthropic key (right-click to paste), then Enter: "
set "ANTHROPIC_WORKSPACE_ID="
set /p "ANTHROPIC_WORKSPACE_ID=Workspace id (wrkspc_...), or just press Enter if your key is workspace-scoped: "
set "CODE=none"
if defined ANTHROPIC_WORKSPACE_ID goto withws
for /f %%i in ('curl.exe -s -o NUL -w "%%{http_code}" https://api.anthropic.com/v1/models -H "x-api-key: %ANTHROPIC_API_KEY%" -H "anthropic-version: 2023-06-01"') do set "CODE=%%i"
goto checked
:withws
for /f %%i in ('curl.exe -s -o NUL -w "%%{http_code}" https://api.anthropic.com/v1/models -H "x-api-key: %ANTHROPIC_API_KEY%" -H "anthropic-workspace-id: %ANTHROPIC_WORKSPACE_ID%" -H "anthropic-version: 2023-06-01"') do set "CODE=%%i"
:checked
echo Key check returned HTTP %CODE%
if not "%CODE%"=="200" goto badkey

echo.
echo === Real run ===
"%PY%" "%RUNNER%" "%SEED%" --out "%WORK%\result.json" --confirm-spend
echo.
echo Runner exit code: %ERRORLEVEL%
echo Result file: %WORK%\result.json
echo.
echo === Did the agents actually act? ===
"%PY%" "%~dp0oasis_check.py" "%WORK%\oasis_fc6ca01860ab.db" "%WORK%\log"
echo.
echo Tell Claude it has finished. If anything above says NOTHING or FAILED, copy it to Claude.
goto done

:badkey
echo Not running: 400 = workspace id wrong or missing, 401 = key mistyped or invalid. Nothing was spent.
goto done
:nopy
echo Cannot find the OASIS Python: %PY%
goto done
:noseed
echo Cannot find the seed file: %SEED%
goto done
:nosaved
echo Cannot find the saved runner: %SAVED%
goto done

:done
set "ANTHROPIC_API_KEY="
set "ANTHROPIC_WORKSPACE_ID="
echo.
pause
endlocal
