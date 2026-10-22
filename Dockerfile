# DataPilot 后端（FastAPI）。数据库不进镜像：运行时把 ./data 只读挂载到 /app/data。
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY evals ./evals
RUN uv sync --frozen --no-dev
EXPOSE 8000
CMD ["uv", "run", "--frozen", "--no-sync", "uvicorn", "datapilot.api:app", "--host", "0.0.0.0", "--port", "8000"]
