# 生产/演示都用这一份镜像：Linux 里没有 Windows Smart App Control 那类原生扩展拦截，
# 本机跑不起来的问题在镜像里不存在。
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先只装依赖，代码改动不会打破这层缓存
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY travel/ ./travel/
COPY evals/ ./evals/
COPY data/ ./data/
COPY web/ ./web/
COPY service.py main.py ./

# 非 root 运行；resources 挂出来，会话库不落在镜像层里
RUN useradd -m -u 10001 app && mkdir -p /app/resources && chown -R app:app /app
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8080"]