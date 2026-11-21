FROM ghcr.io/prefix-dev/pixi:0.84.1
WORKDIR /app
COPY pixi.toml pixi.lock pyproject.toml ./
COPY src ./src
RUN pixi install --locked
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY tests/fixtures ./tests/fixtures
RUN useradd --uid 10001 --create-home chaser && mkdir -p /app/.data && chown -R chaser /app
USER chaser
EXPOSE 8000
CMD ["pixi", "run", "--locked", "uvicorn", "supplier_chaser.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
