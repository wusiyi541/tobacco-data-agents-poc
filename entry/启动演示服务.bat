@echo off
REM ============================================================
REM  数据共享中心智能体 - 一键启动全部演示服务
REM  必须保持 GBK/ANSI 编码 + CRLF 换行, 否则双击无反应
REM  前置: Docker Desktop 已启动 (Dify 控制台 8070)
REM ============================================================
set PY=C:\Users\49241\.workbuddy\binaries\python\versions\3.13.12\python.exe
set PYMPL=C:\Users\49241\.workbuddy\binaries\python\envs\demo\Scripts\python.exe
REM BASE = 本脚本所在目录的上一级 (即 省烟草智能体)
for %%I in ("%~dp0..") do set "BASE=%%~fI"

if not exist "%PY%" (
  echo [错误] 找不到 Python: %PY%
  pause
  exit /b 1
)
if not exist "%PYMPL%" (
  echo [警告] 找不到带 matplotlib 的 Python: %PYMPL%
  echo        血缘服务 8091 会启动失败, 其它六个不受影响.
  echo.
)
if not exist "%BASE%\入口" (
  echo [错误] 工作目录不存在: %BASE%
  pause
  exit /b 1
)

echo 工作目录: %BASE%
echo.
echo [0/7] 清理端口 8090-8096 残留进程 ...
for %%P in (8090 8091 8092 8093 8094 8095 8096) do (
  for /f "tokens=5" %%A in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":%%P "') do taskkill /PID %%A /F >nul 2>&1
)

echo [1/7] 血缘渲染服务 8091 ...
start "dq-8091" /min cmd /c "cd /d %BASE%\智能寻数智能体\poc && %PYMPL% -u lineage_server.py"

echo [2/7] 元数据补充智能体 8092 ...
start "meta-8092" /min cmd /c "cd /d %BASE%\元数据页面改造 && %PY% -u poc_server.py"

echo [3/7] 模型符合性检查 8093 ...
start "rule-8093" /min cmd /c "cd /d %BASE%\模型符合性检查智能体\poc && %PY% -u poc_server.py"

echo [4/7] 需求追踪 Mock API 8094 ...
start "req-8094" /min cmd /c "cd /d %BASE%\需求追踪智能体\poc && %PY% -u mock_api.py --port 8094"

echo [5/7] 智能寻数演示服务 8095 ...
start "find-8095" /min cmd /c "cd /d %BASE%\智能寻数智能体\poc && %PY% -u demo_server.py"

echo [6/7] 数据质检演示服务 8096 ...
start "dqc-8096" /min cmd /c "cd /d %BASE%\数据质检智能体\poc && %PY% -u demo_server.py"

echo [7/7] 数据质检 Mock API 8090 ...
start "dqmock-8090" /min cmd /c "cd /d %BASE%\数据质检智能体\poc && %PY% -u mock_api.py --port 8090"

echo.
echo 等待 6 秒后自检端口 ...
timeout /t 6 /nobreak >nul

for %%P in (8090 8091 8092 8093 8094 8095 8096) do (
  netstat -ano | findstr "LISTENING" | findstr ":%%P " >nul && (
    echo   [OK]   %%P
  ) || (
    echo   [FAIL] %%P  - 服务未起来, 看对应最小化窗口的报错
  )
)

echo.
start "" "%BASE%\入口\index.html"
echo 已打开演示入口. 服务在独立最小化窗口运行, 关闭本窗口不影响.
echo 停止服务请运行 停止演示服务.bat
echo.
pause
