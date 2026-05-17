# 冬 · 项目知识库

## 项目概述
冬是一个运行在QQ上的AI机器人，具备人格模拟、对话管理、工具调用、代码自修复能力。

## 如何启动
运行 `启动.bat`，脚本会：
1. 启动 NapCat QQ Bot 框架
2. 启动冬的主循环 `python -m dong`
3. 冬自动连接 QQ WebSocket 并开始处理消息

## 核心模块职责
| 文件 | 职责 |
|------|------|
| dong/__init__.py | 主循环 robot_loop()，消息分发入口 |
| dong/agent.py | 智能体引擎 /d 命令处理 + 工具调用 |
| dong/config.py | 配置管理（API密钥、白名单等） |
| dong/memory.py | 记忆系统（长期/短期/会话） |
| dong/media.py | 媒体处理（图片下载/识别/语音） |
| dong/tools.py | 桌面控制工具（截图/点击/输入） |
| dong/interaction.py | 对话生成和回复处理 |
| dong/status.py | 状态管理（已迁移到 status/ 包） |
| dong/status/ | 状态系统子包（心情/天气/激素） |
| dong/bridge/ | QQ→Claude桥接（/d c 命令） |
| dong/core/ | 核心基础设施（API网关/熔断/健康检查） |
| dong/schedule.py | 日程管理 |
| dong/amygdala.py | 杏仁核快速情感通路 |
| dong/decision.py | 决策引擎 |
| dong/dialogue_evaluator.py | 对话质量评估 |

## 常用命令
| 命令 | 功能 |
|------|------|
| /d fix <问题> | 自动定位+修改+验证 |
| /d review <文件> | 代码审查 |
| /d validate | 编译验证 |
| /d diff | 查看最近修改的diff |
| /d changed | 查看变更摘要 |
| /d status | 当前任务状态 |
| /d index <符号> | 查函数/类定义和调用关系 |
| /d refs <函数> | 查引用关系 |
| /d knowledge | 显示知识库 |

## 设计禁区
- 不能动 __init__.py 的 robot_loop 函数
- 不能删 status.py（已迁移到 status/ 包，但保留存根）
- 不能修改 .env 文件
- 不能动 bridge/ 下的文件（Claude桥接独立维护）
