ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b

FROM ${PYTHON_IMAGE} AS python-base

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore


FROM python-base AS dependency-wheels

COPY docker/constraints.txt /constraints.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --requirement /constraints.txt --wheel-dir /wheels


FROM python-base AS app-wheel

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --no-deps setuptools==80.9.0

WORKDIR /src

COPY pyproject.toml README.md image_service.py zvec_logging.py ./
COPY image_vector_service ./image_vector_service

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --no-build-isolation --no-deps --wheel-dir /wheels .


FROM python-base AS runtime-filesystem

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home \
        --shell /usr/sbin/nologin app \
    && mkdir -p /data/workspace /data/results /data/roots/main /data/query \
    && chown -R app:app /data/workspace /data/results /home/app

RUN --mount=type=bind,from=dependency-wheels,source=/wheels,target=/dependency-wheels \
    --mount=type=bind,from=dependency-wheels,source=/constraints.txt,target=/constraints.txt \
    --mount=type=bind,from=app-wheel,source=/wheels,target=/app-wheels \
    --mount=type=bind,source=docker/prune_runtime.py,target=/tmp/prune_runtime.py,readonly \
    python -m pip install --no-compile --no-index \
        --find-links=/dependency-wheels --requirement /constraints.txt \
    && python -m pip install --no-compile --no-deps --no-index \
        --find-links=/app-wheels /app-wheels/zvec_image_search-*.whl \
    && python -m pip check \
    && python /tmp/prune_runtime.py \
    && python -c "import numpy, zvec; from PIL import BmpImagePlugin, IcnsImagePlugin, IcoImagePlugin, JpegImagePlugin, PngImagePlugin, SgiImagePlugin, TiffImagePlugin, WebPImagePlugin" \
    && zvec-image-search --help > /dev/null


FROM scratch AS runtime

# Copying the pruned filesystem into scratch removes deleted base-layer files.
COPY --from=runtime-filesystem / /

LABEL org.opencontainers.image.title="zvec-image-search" \
      org.opencontainers.image.description="Local multimodal image search CLI with DashScope and Zvec"

ENV LANG=C.UTF-8 \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    ZVEC_IMAGE_WORKSPACE=/data/workspace \
    ZVEC_IMAGE_RESULTS_DIR=/data/results

USER app:app
WORKDIR /home/app

ENTRYPOINT ["zvec-image-search"]
CMD ["--help"]
