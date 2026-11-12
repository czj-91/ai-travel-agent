# Changelog

记录对本项目**可验证**的变更（含被证伪的结论），不写空话。

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
