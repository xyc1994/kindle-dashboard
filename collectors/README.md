---
title: Kindle 工作控制台 · 采集器规划
created: 2026-09-03
updated: 2026-09-03
type: implementation-plan
status: draft
related_to:
  - "[[../README]]"
  - "[[../architecture/数据汇聚与更新规则]]"
---

# 采集器规划

## 共同契约

每个采集器只负责“读取一种来源并标准化”，不得直接生成 Kindle 图片，也不得自行长期维护独立缓存文件。

统一接口概念：

```python
class Collector:
    name: str

    async def collect(self, context) -> CollectorResult:
        ...
```

`CollectorResult` 包含来源名、采集时间、对象列表、健康状态和脱敏错误摘要。Scheduler 收到完整结果后使用一次 SQLite 事务替换该来源的当前对象。

## 计划模块

### `weather_qweather`

- 调用和风天气 v1 当前天气与每日预报。
- 每 2 小时运行一次，启动后立即运行。
- 认证信息只从环境变量或 Windows 凭据管理器读取。
- 网络失败时返回失败状态，不返回空成功结果。

### `obsidian_tasks`

- 直接只读 Markdown，不要求 Obsidian 客户端保持打开。
- 解析结构化 Memo frontmatter。
- 扫描带 `#kindle` 的日记 checkbox。
- 为每条任务保存稳定 ID、相对路径、行号和源内容 hash。
- 第一阶段不得写回 Vault。

### `codex_activity`

- 优先启动本机 `codex app-server --stdio`。
- 读取线程状态、活动标志、额度与重置时间。
- app-server 不可用时，降级读取本机 Codex 状态数据库和会话记录。
- 输出内容必须截断并脱敏，不把完整会话写入仪表盘数据库。

### `kindle_status`

- 接收 Kindle 电量、充电状态、当前页面和最后成功渲染时间。
- 校验设备令牌和字段范围。
- 不接受任意命令、文件路径或脚本内容。

## 调度约束

- Scheduler 是唯一数据库写者。
- 相同采集器同一时间最多运行一个实例。
- 超时后取消本轮，保留上次成功结果。
- 手动刷新只绕过时间计划，不绕过并发锁、速率限制和认证检查。
- 只有规范化数据 hash 变化时才通知 Renderer。

