@echo off
REM Kindle 看板 PC 服务端 —— 开机自启脚本
REM 用法：把本文件放进「启动」文件夹（Win+R 输入 shell:startup 打开），登录后自动拉起服务。
REM 服务常驻，每次 Kindle 拉图自动重建看板（时间实时）；关掉此窗口即停服。

REM ====================================================================
REM  【改完代码必须重启本服务！】  —— 见 2026-09-04 踩坑记录
REM  本服务在「启动时」就把 build_dashboard / dashboard_renderer / collectors
REM  等模块加载进进程内存；之后改了 src/ 下任何渲染/采集/构建代码，Python
REM  不会在每次拉图时重新读磁盘。即便开着 --auto-build，设备拉到的仍是旧版。
REM  现象：Kindle 屏上布局/数据没变，但本地 samples/preview_home.png 已更新。
REM  处理：改完 src/ 后，先 taskkill 掉旧 dash_server 进程，再双击本 bat 重启，
REM        设备下次拉图（≤60s）即拿到新布局。
REM ====================================================================

cd /d "E:\BaiduSyncdisk\02_Projects\Hobbies\AI造物\kindle改造"

set PY="C:\Users\houxu\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set IMG="samples\preview_home.png"

echo [Kindle Dash] 启动 PC 服务端，端口 8765，按 Ctrl+C 停止
%PY% src\dash_server.py --image %IMG% --port 8765 --auto-build
