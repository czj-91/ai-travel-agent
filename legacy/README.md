# legacy

课程 v1 阶段的单文件版本（原 `personal_chief.py`，主题：私人厨师）。
项目主题已切换到旅行规划，主实现见 `../travel/`，这里只保留作演进记录。

其中的 `base_url=os.getenv(...)` 没有做校验，留空会让 OpenAI SDK 静默回退到
`api.openai.com` —— 该问题已在 `travel/config.py` 修复，并由
`tests/test_offline.py::test_require_env_and_model_gating` 锁住。