---
title: Mibslee/kindledashboard 项目调研
created: 2026-09-03
updated: 2026-09-03
type: reference
source: "https://github.com/Mibslee/kindledashboard"
tags:
  - Kindle
  - FBInk
  - KUAL
related_to:
  - "[[../README]]"
---

# Mibslee/kindledashboard 项目调研

## 来源

- GitHub：[Mibslee/kindledashboard](https://github.com/Mibslee/kindledashboard)
- 目标设备与本项目一致：Kindle Paperwhite 3，竖屏 `1072 × 1448`。

## 原项目原理

1. macOS 菜单栏程序收集天气、日历、音乐、Codex 和系统状态。
2. Swift 渲染器生成 SVG/PNG。
3. 本地 HTTP 服务在 `8787` 端口提供 `frame.png` 和控制信息。
4. Kindle KUAL 扩展通过 `wget/curl` 下载画面。
5. FBInk 使用 GL16/GC16 模式将 PNG 绘制到电子墨水屏。
6. Kindle 向电脑回传电量和充电状态。

## 可直接继承

- Kindle 端 KUAL 菜单结构和启动/停止流程。
- `control.json` + `frame.png` 的拉取模型。
- FBInk 局部刷新与周期全刷策略。
- Kindle 电量回传和电脑离线时保留最后画面。
- 仅停止原生 statusbar、避免破坏整个 Kindle framework 的 clean dashboard 策略。

## 需要重做

- macOS Swift/AppKit 控制端改成 Windows 后台服务。
- AppleScript、Music 和 Mac 系统信息不进入第一阶段。
- 天气改为和风天气 API；不再使用 `wttr.in`。
- Obsidian 替代原项目日历/提醒来源。
- Codex 状态改用真实 thread status 和 active flags，不再只用最近用户消息推测。
- 新增低风险触摸区；原项目不包含 Kindle 触摸事件监听器。

## 风险

- 原项目 HTTP 服务无强认证，只适合可信局域网；本项目必须增加设备令牌。
- Kindle 触摸输入可能与原生顶部菜单冲突，只接管底部低风险区域。
- Codex app-server 是实验接口，应封装适配器并保留降级路径。
- 上游源码作为参考，不在 Vault 内复制完整仓库；实现时保留许可证和来源说明。

