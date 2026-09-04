# Kindle 看板 · 换网络/换地方维护指南

适用：`dash.sh` 已部署「自动发现」版（按需扫描，**平时拉图不扫网段**，只在启动或连续取图失败 3 次时扫）。

## 首次上屏（现在就做）
1. 确保 PC 服务端在跑（见下方「PC 端」）。
2. Kindle 与 PC 连**同一个 wifi**。
3. 长按电源键 **7 秒硬重启** Kindle → 重启后 KUAL 点「8.启动看板」。
   - 新脚本启动时扫本网段自动找到 PC，约 10~20 秒后上屏。

## 以后每次「换 wifi / 换地方」
**方式 A（全自动，推荐）**：什么都不用改。
- 在 Kindle 上重启看板（KUAL「9.停止看板」→「8.启动看板」，或长按电源键硬重启）即可。
- 启动时自动发现会扫新网段找到 PC 当前 IP，缓存到 `dashboard/pc_ip`，之后不再扫描。

**方式 B（让 agent 直接跑，更快/可控）**：
- 用 USB 把 Kindle 连到 PC（数据传输模式，盘符 `F:`）。
- 对 agent 说：「换 wifi 了，更新 HOST」或「重新连一下」。
- agent 会执行：探测 PC 当前局域网 IP → 写入 `F:/dashboard/server.conf` 的 `HOST=` → 清掉旧 `pc_ip` 缓存 → 提示你重启看板。

## 退出看板
- USB 连 PC → 在 Kindle 盘 `dashboard/` 里新建空文件 `stop`（无扩展名）→ 安全弹出拔线 → 看板下一轮（≤60 秒）自动退出，原生界面回来（framework 没停，KUAL 仍可进）。

## PC 端（一次性）
- 服务：`tools/start_dash_server.bat` 放「启动」文件夹（`shell:startup`）开机自启，监听 `8765`。
- token：`C:/Users/houxu/.dash_server/token`（已自动填进 `server.conf`，勿改）。

## 验证上屏成功
- 屏上出现看板，且能进 KUAL、触屏可用、能看其他书（不停 framework）。
- PC 端：`curl http://localhost:8765/health` 里 `kindle_battery` 变成**真实电量**（非 88% 模拟值）= 成功。

## 故障排查
- 屏不亮 / 取图失败：看 `F:/dashboard/dash.log` 末几行。
  - `discover: 未发现 PC` → 确认 PC 服务在跑、Kindle 与 PC 同 wifi。
  - `取图失败` 连续出现 → 等 3 次后自动重扫；仍不行按方式 B 让 agent 修正 HOST。
- 书架叠加：本方案不停 framework，靠 15 秒重绘压住原生 UI，属正常；重绘间隙可能短暂闪一下（方案固有现象）。

## 关键文件
- `kindle_ext/dash.sh` —— Kindle 端主循环（已部署到 `F:/extensions/kdashboard/dash.sh`）
- `kindle_ext/server.conf.example` —— 配置样例
- `tools/start_dash_server.bat` —— PC 端开机自启
- `src/dash_server.py` / `src/build_dashboard.py` / `src/dashboard_renderer.py` —— PC 端渲染与接口
