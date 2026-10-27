@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 双击：渲染内置示例并打开
rem 拖拽：把一个 .md 文件拖到本文件上，就渲染那一个

set "TARGET=%~1"
if not defined TARGET set "TARGET=samples\示例回复.md"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo 没找到 python。
  echo 请先安装 Python 3.8 或更高版本，安装时勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

echo 正在渲染：%TARGET%
echo.
python tools\gloss.py "%TARGET%"
if errorlevel 1 (
  echo.
  echo 渲染失败，请看上面的报错。
  pause
  exit /b 1
)

for %%I in ("%TARGET%") do set "OUT=%%~dpnI.gloss.html"
start "" "%OUT%"
