---
title: Kindle 工作控制台 · Current State
type: current-state
scope: project
updated: 2026-09-03T00:51:00+08:00
updated_by: Codex
source_refs:
  - "[[README]]"
  - "[[product-spec/显示与交互设计]]"
  - "[[architecture/数据汇聚与更新规则]]"
---

# Kindle 工作控制台 · Current State

## 已完成

- Kindle Paperwhite 3 已完成越狱、Hotfix、KUAL、MRPI 与 KOReader 基础安装。
- 已分析 `Mibslee/kindledashboard` 的 Windows 迁移范围、刷新机制和交互限制。
- 已确认竖屏、同一局域网、Obsidian 待办、Codex 状态与额度等需求。
- 已确认天气为上海市黄浦区，使用和风天气 API，每 2 小时采集一次。
- 已确定 SQLite 单一事实源、单写者调度器和派生 PNG 的数据架构。
- 已确认项目目录中存在 `QWeather-Icons-1.8.0`，后续直接复用其 SVG 与许可证。
- 已建立项目入口、产品设计、数据更新规则、配置示例和采集器规划。

## 当前

- 状态：设计完成，等待进入 Windows 服务最小可运行版本开发。
- 第一阶段保持 Obsidian 只读，不直接修改百度同步盘中的任务文件。

## 下一步

1. 建立 Windows Python 服务、SQLite schema 和统一采集器接口。
2. 接入和风天气 v1 当前天气与 3 日预报。
3. 接入 Obsidian 结构化 Memo 和带 `#kindle` 的日记任务。
4. 接入 Codex app-server 的额度、线程状态和人工介入标志。
5. 生成首张真实 `1072 × 1448` PNG，并在电脑端验证。
6. 最后安装 Kindle 端扩展并验证局部/全局刷新。

## 阻塞与风险

- 尚未配置和风天气专属 API Host 与认证凭据；凭据必须留在 Vault 外。
- `05_Memos/Memos_Active` 和 `Memos_Archive` 当前表现为百度同步盘按需目录，部署前需设为始终保留在本机。
- Codex app-server 属实验接口，需要适配器隔离与本地数据库降级方案。
- Kindle 触摸写回和 Codex 审批具有误操作风险，第一阶段不启用。

## 验证状态

- 路径、项目入口和内部 Wikilink 已规划。
- 和风天气当前 v1 接口与认证方式已根据官方资料核对；黄浦区坐标与兼容 LocationID 将在首次 API 联调时通过 GeoAPI/响应元数据再次确认。
- 尚未进行 API 联调、数据库迁移测试、PNG 像素验证和真机刷新测试。

## 状态日志 (Log)

- [x] [Codex] 2026-09-03 00:51: 完成项目建档、产品范围确认和统一数据架构设计。
- [ ] [Codex] 待处理: 实现 Windows 最小可运行版本并生成首张真实看板画面。
