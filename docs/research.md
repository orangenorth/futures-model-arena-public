# 同类开源项目调研：多模型 AI 交易竞赛

检索时间：2026-09。目标：为"四模型期货模拟竞赛桌面软件"找可借鉴的功能与设计。

## 一、项目一览

| 项目 | Star | 技术栈 | 定位 |
|---|---|---|---|
| [HKUDS/AI-Trader](https://github.com/HKUDS/AI-Trader) | 22.4k | Python FastAPI + React，PostgreSQL / SQLite | 学术基准 + 竞技场：多模型在 NASDAQ100 / SSE50 / 加密市场以相同条件竞赛 |
| [NoFxAiOS/nofx](https://github.com/NoFxAiOS/nofx) | 12.9k | Go + React/TypeScript | 多交易所、多模型实盘/模拟自动交易 OS，内置竞赛与自进化 |
| [wquguru/nof0](https://github.com/wquguru/nof0) | 2.8k | Go-Zero + Next.js 15 / Recharts | nof1.ai Alpha Arena 的开源复刻 |
| [HammerGPT/Hyper-Alpha-Arena](https://github.com/HammerGPT/Hyper-Alpha-Arena) | 1.2k | Node + React + PostgreSQL，Docker | Hyperliquid / 币安合约的 AI 竞技场，含因子库与归因 |
| [SnowingFox/open-nof1.ai](https://github.com/SnowingFox/open-nof1.ai) | 808 | Next.js + Prisma + Recharts + CCXT | Alpha Arena 开源实现，DeepSeek 驱动 |
| [etrobot/open-alpha-arena](https://github.com/etrobot/open-alpha-arena) | 596 | Hono + SQLite(Drizzle) + React | 纯 TypeScript 的模拟盘竞赛 |
| [kukapay/awesome-alpha-arena](https://github.com/kukapay/awesome-alpha-arena) | — | 索引 | 18 个 Alpha Arena 克隆/衍生项目汇总 |

原型：nof1.ai **Alpha Arena**（2025-10 赛季）——6 个前沿模型各持 $10k 在 Hyperliquid 永续合约实盘竞赛，
相同 prompt、相同输入，分钟级固定间隔喂入 K 线 + 技术指标（EMA/RSI/MACD/ATR）+ 资金费率/持仓量 + 账户状态，
模型输出结构化 JSON（每品种 hold / open_long / open_short / close，含杠杆、仓位比例、止损止盈、信心度、理由）。
排行指标：收益率、Sharpe、最大回撤、胜率、交易次数、平均持仓时长。

## 二、各项目核心功能与设计

### HKUDS/AI-Trader
- 所有模型使用**相同资金、数据、工具与评估指标**，保证公平对比。
- 模拟盘 $100k 起始，逐笔 mark-to-market；排行榜指标：收益率、Sharpe、最大回撤。
- 决策全程自动：市场研究 → 决策 → 交易日志 → 策略自适应。
- 论文结论：通用智能不等于交易能力；**风控能力决定跨市场稳健性**；高流动性市场更易获得超额收益。

### NoFxAiOS/nofx
- 7 个模型（DeepSeek/Qwen/Claude/GPT/Gemini/Grok/Kimi）并行竞赛，按已实现收益排行。
- **模型只"提议"，运行时强制硬约束**：最大并发仓位、名义占权益比例上限、每品种单仓、杠杆硬上限、
  每笔必带交易所侧止损止盈、回撤自动平仓、最短持仓时间与再入场冷却、连续失败进入安全模式。
- 每几分钟扫描一次市场；所有决策连同推理落库。
- 看板：实时持仓、订单、统计、仓位历史、策略编辑器、行情图、配置界面。

### wquguru/nof0
- 每模型 $10k 起始，实时 PnL；强调"用真实竞赛结果迭代 prompt 而非回测"。
- 看板结构：权益曲线、持仓总览、成交记录、模型"对话"页、按 PnL 排行、单模型详情页。

### HammerGPT/Hyper-Alpha-Arena
- 多交易员竞技场：多策略资产曲线同图对比；交易归因（按品种 / 触发类型 / 因子 / 时段）。
- 86 个内置因子 + IC/ICIR 评分喂给 LLM；测试网模拟盘与主网一键切换。

### SnowingFox/open-nof1.ai
- 指标每 20 秒采样、AI 每 3 分钟决策；prompt / 推理链 / 执行结果全落库。
- 看板：实时价格、权益曲线、已完成交易、当前持仓、"模型思考"透明页。

### etrobot/open-alpha-arena
- 单文件 SQLite 本地持久化；OpenAI-compatible 接口接任意模型；模拟盘支持杠杆。

## 三、提炼出的设计原则（本项目采用）

1. **公平性**：四模型完全相同的 prompt 模板、行情快照、起始资金、费率、风控参数。
2. **模型只提议，引擎裁决**：LLM 输出 JSON → 校验 → 硬风控（杠杆上限、单品种单仓、最大持仓数、必带止损、保证金校验）→ 模拟撮合。
3. **全链路落库**：每轮的 prompt、原始回复、解析后的动作、接受/拒绝原因、成交、权益快照全部写 SQLite，看板可回放。
4. **单模型失败不影响竞赛**：超时 / 非法 JSON → 记为 HOLD + 错误日志；连续失败进入熔断、自动恢复。
5. **看板结构**：排行榜 + 权益曲线对比 + 单模型详情（持仓 / 成交 / 决策日志）+ 阶段性结果。
6. **本地优先**：SQLite + 静态前端，无外部服务依赖，关闭重开即续跑。
