FROM ghcr.io/prefix-dev/pixi:0.84.1
# Own files at copy time: a later `chown -R` duplicates the whole environment in a new layer.
RUN useradd --uid 10001 --create-home chaser && mkdir -p /app/.data && chown -R chaser /app
WORKDIR /app
USER chaser
COPY --chown=chaser pixi.toml pixi.lock pyproject.toml ./
COPY --chown=chaser src ./src
# Tesseract ships ~125 language packs; keep the OCR_LANGUAGES default plus orientation data.
# Cleanup shares the install layer so the dropped files never reach the image.
RUN pixi install --locked \
    && find .pixi/envs/default/share/tessdata -name '*.traineddata' \
        ! -name eng.traineddata ! -name deu.traineddata ! -name pol.traineddata \
        ! -name kor.traineddata ! -name chi_sim.traineddata ! -name osd.traineddata -delete \
    && rm -rf /home/chaser/.cache
COPY --chown=chaser alembic.ini ./
COPY --chown=chaser migrations ./migrations
COPY --chown=chaser scripts ./scripts
COPY --chown=chaser tests/fixtures ./tests/fixtures
EXPOSE 8000
CMD ["pixi", "run", "--locked", "uvicorn", "supplier_chaser.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
