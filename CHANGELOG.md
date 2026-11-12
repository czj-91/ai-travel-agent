# Changelog

记录对本项目**可验证**的变更（含被证伪的结论），不写空话。

## [0.8.1] - 2026-11-12

修复
- `writing` 进度事件跨重试累加：实测苏州请求里结构化工具出现了两次 `response`（第一次被判无效后重试），
  导致 `chars` / `days_seen` 报出虚高进度。改为每次新的结构化工具调用重新计数。
- 流式接口原先把异常吞成「服务端异常」且不留日志：现在服务端 `_logger.exception` 记录完整堆栈，
  对外仍只给异常类型，不泄露内部信息。

验证：新代码在进程内跑苏州请求通过（`provenance = {local_kb: 0, web: 6, weather_days: 4, calls: 3}`，
本地库无苏州数据时正确转为联网依据）；而 8080 上未重启的旧后端同一请求返回 error，
说明失败来自旧进程，不是新代码。

测试：47 离线 + 18 服务层 + 4 会话库 + 3 指标 = 72 全过。

## [0.8.0] - 2026-11-12

新增
- 数据新鲜度面板：`provenance_from_messages()` 从服务端工具消息里数出
  「本地权威库 / 实时天气 / 联网搜索 / 已过 TTL」四类依据，附在 `plan` 事件与 `/plan` 响应上。
  实测北京请求得到 `{local_kb: 4, weather_days: 4, web: 0, stale: 0, calls: 3}`——本地库够用就不联网。
- 结构化 JSON 抽屉（`<details>` + 复制 JSON），证明输出可被程序直接消费。
- 宽屏三栏：≥1400px 且已有结果时切「输入 / 结果 / 历史」三栏；无结果时保持两栏。
- 字号从 px 改为 rem（31 处），跟随用户系统字号设置。
- 历史空态加「先载入示例行程看看效果」引导按钮。
- `plan_trip(return_messages=True)`，让服务层能拿到本轮消息算依据来源。

修复
- `provenance_from_messages` 第一版把结构化工具 `TripPlan` 的返回值也当成外部依据计数（虚报 1 次），
  现在只认真工具；新增测试断言 `calls == 3`。
- 前端剪贴板逻辑此前在 `copyMarkdown` 里内联一份，抽出 `writeClipboard()` 与「复制 JSON」共用。

测试：46 离线 + 18 服务层 + 4 会话库 + 3 指标 = 71 全过；ruff check 与 format --check 全绿。

## [0.7.0] - 2026-11-12

新增
- 工具执行轨迹：`tool_result` 事件带 `ms` / `hits` / `chars`，结果区展示每个工具花多久、命中几条、返回多少字符。
  实测一次厦门请求：poi_lookup 753 ms（2 条）、weather_forecast 2,525 ms（4 条）、web_search 3,552 ms（3 条）、
  生成结构化方案 8,695 ms。
- 成本角标：`Settings.price_in_per_m / price_out_per_m`（每百万 token 金额），`cost_yuan()` 估算本次花费，
  `/plan` 与 `plan` 事件都带上。实测 ¥0.0297。**没配单价返回 None，界面不显示，绝不瞎猜价格。**
- `start` 事件带上模型名，前端无需额外请求就知道当前跑的是哪个模型。
- 前端「复制 Markdown」：剪贴板 API 在非安全上下文不可用时退回临时 textarea 方案。

修复
- `Settings` 新增单价字段带默认值 0.0，避免新增配置项让既有构造点（含测试）全部崩掉。
- 结构化工具 `TripPlan` 的 `tool_result` 会混进执行轨迹，看起来像外部服务：
  事件加 `kind: "response" | "tool"`，前端把方案生成单独列一行。

测试：45 离线 + 18 服务层 + 4 会话库 + 3 指标 = 70 全过；ruff check 与 format --check 全绿。

## [0.6.0] - 2026-11-12

新增
- SSE `writing` 进度事件：累计结构化工具的 args 分片，报出「已写多少字符、写到第几天」。
  实测分片确实流式到达（238 片 / 348 字符），所以不需要额外模型调用就有真实进度。
- 前端可取消生成（`AbortController` + 取消按钮）。
- 前端错误分类：401 自动展开令牌框、402 说明余额、422 归为输入、429 读 `Retry-After` 做按钮倒计时、
  502 说明已兜底过一次可重试、网络失败提示确认服务。
- 图片投放区按 `/health` 返回的模型名做能力门控；服务令牌字段默认隐藏（`?admin=1` 或 401 时出现）。
- 可访问性：进度文案 `aria-live="polite"`、统一 `:focus-visible` 焦点环、投放区禁用态样式。
- 底部接口清单补上实际使用的 `POST /plan/stream`。

修复
- `days_seen` 第一版只统计当前分片，导致进度恒为 0；改为对累计 args 计数。
- 测试断言 `writings[0] < writings[-1]` 在单元素列表上自比恒假；改为先断言事件数量。

测试：42 离线 + 18 服务层 + 4 会话库 + 3 指标 = 67 全过；ruff check 与 format --check 全绿。

## [0.5.0] - 2026-11-12

新增
- 会话库清理 `travel/memory.py`：按 thread 前缀清理评测/调试会话，默认 dry-run，每个前缀保留最近 N 个。
  本机实测 `checkpoint.db` 从 **30.82 MB 降到 2.35 MB**（删除 133 个 eval thread / 6527 行）。
- 混合检索 `PoiIndex.search_hybrid()`：用 RRF（Reciprocal Rank Fusion）融合向量与关键词两路名次，
  不再只是「embedding 不可用时降级」。
- 检索质量指标 `evals/retrieval.py` + 20 条人工标注查询（Recall@k / Precision@1 / nDCG@k）。
  实测 keyword 与 embedding 均为 0.950 / 0.950 / 0.950，hybrid(RRF) 为 **1.000 / 1.000 / 1.000**。
- 并发批量评测 `evals/run_eval.py --parallel N`。
- 工程化：`ruff.toml`、`.pre-commit-config.yaml`、本文件、CI 增加 lint job。

修复
- `ndcg_at_k` 的 IDCG 误用**升序**排序，导致理想增益被排到末尾、nDCG 算出 1.85 这种超过上限 1 的荒谬值；
  并补上检索结果去重（重复文档会让 nDCG 达到 2.13）。两条都加了回归测试。
- 测试期望本身写错两处：3 个 eval thread 且 `keep_recent=1` 应剩 **1** 个（我断言成 2）；
  「相关文档全命中时换序 nDCG 应 <1」是错的——增益全为 1 时排列不改变 DCG。
- Windows 下 `TemporaryDirectory` 清理竞态导致测试偶发 `PermissionError`：改用 `ignore_cleanup_errors=True`。

## [0.4.0] - 2026-11-11

新增
- 评测扩到 20 条用例；LLM-as-judge（`travel/judge.py`）；三臂对比实验（`evals/compare.py`）。
- 事实核对器 `fact_check_vs_kb`：改为名称 bigram 相似度 >= 0.55 才参与判定。
  收紧前 20 条评测报出 5 条「门票价不一致」，逐条核对**至少 4 条是误报**
  （把「锦里」当成「武侯祠与锦里」、把「三塔倒影公园（外围机位）」当成收费景区）；
  误报样本已固化成回归测试，收紧后同批结果为 **0**。

修复
- `with_structured_output` 返回对象而非消息，导致对比实验 C 臂统计到 0 token、被误判成「6/6 失败」；
  改用 `include_raw=True` 后结论才成立：格式违规被消除，剩余失败集中在缺来源。

## [0.3.0] - 2026-11-09

新增
- SSE 流式接口 `/plan/stream` 与前端流式消费（不支持时自动退回 `/plan`）。
- 安全基线：外部内容 `<external_data>` 定界并中和闭合标签越狱、IP 速率限制（429 + `Retry-After`）、
  CORS 白名单、异常信息不外泄。
- Dockerfile（非 root + healthcheck）、`.dockerignore` 排除 `.env`、compose；
  CI 增加镜像构建与「镜像内不得出现 .env」校验。

修复
- `SqliteSaver` 不支持 async：`astream` 抛 `NotImplementedError`，流式改为同步生成器 + `graph.stream`。
- 速率限制器污染既有测试（14/14 掉到 9/14 全是 429）：`RateLimiter` 加 `reset()` 并在测试夹具清零，
  同时补一条专门验证 429 的测试。
- `stream_plan` 用了 `json.loads` 却漏 `import json`：`py_compile` 与导入都发现不了，靠 SSE 测试断言抓出。

## [0.2.0] - 2026-11-09

新增
- 本地 POI 知识库 RAG（21 条语料、向量召回 + 关键词兜底、相似度阈值按实测分布标定、TTL 过期提示）。
- 实时天气工具（Open-Meteo，免密钥）：降水/温度/日出日落 + 城市坐标缓存。
- FastAPI 服务层 `/health` `/plan` `/export` 与网页前端 `web/`；行程导出 txt / Markdown / ics（RFC 5545 字节折行与转义）。
- 多模态输入 `--image`，并在启动阶段拒绝「文本模型 + 图片」的错误组合。

修复
- Studio 图挂自定义 checkpointer 会让 `langgraph dev` 直接 `GraphLoadError` 拒绝启动：
  拆分 `travel/studio.py`，业务模块不再有导入副作用。
- checkpointer 反序列化未注册自定义类型：`JsonPlusSerializer(allowed_msgpack_modules=[...])`。
- token 统计把整个 thread 历史计入，多轮会话单次成本被虚报：改为只统计本轮新增消息。

## [0.1.0] - 2026-11-09

新增
- 旅行规划 Agent 初版：结构化输出契约、工具重试与降级、启动校验、会话记忆、上下文压缩、离线单测与评测脚本。

修复
- `.env` 含中文注释导致 `langgraph dev` GBK 解码崩溃：约定 `.env` 只写 ASCII。
- `os.getenv("DASHSCOPE_BASE_URL")` 为 `None` 时 OpenAI SDK 静默回退 `api.openai.com`：改为启动即校验。
- `load_dotenv()` 依赖当前工作目录：改为按项目根绝对路径加载。
- 传递依赖被直接 import（`langgraph` / `langchain-core` / `pydantic` / `fastapi`）：全部显式声明。
