# syntax=docker/dockerfile:1
FROM ghcr.io/prefix-dev/pixi:0.77.0-bookworm AS build

WORKDIR /app
COPY pyproject.toml pixi.lock ./
COPY src ./src
RUN pixi install --locked --environment prod \
    && pixi shell-hook --environment prod -s bash > /shell-hook.sh \
    && echo 'exec "$@"' >> /shell-hook.sh
# caldav pulls niquests, which pulls urllib3-future; requests pulls stock urllib3.
# Both claim site-packages/urllib3, and urllib3-future ships a .pth that repairs the
# clash by overwriting that directory on interpreter start -- but only when it is
# writable. The runtime stage is non-root over a root-owned env, so that repair can
# never run there and a half-merged tree would be frozen into the image. Settle it
# here, while the tree is still writable, and fail the build if imports stay broken.
RUN /app/.pixi/envs/prod/bin/python -c "import calsync.cli"

FROM debian:bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home calsync

WORKDIR /app
COPY --from=build /app/.pixi/envs/prod /app/.pixi/envs/prod
COPY --from=build /app/src /app/src
COPY --from=build /shell-hook.sh /shell-hook.sh

USER calsync
ENV SYNC_INTERVAL=15m LOG_LEVEL=info PYTHONUNBUFFERED=1

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=2 \
    CMD ["/bin/bash", "/shell-hook.sh", "calsync", "healthcheck"]

ENTRYPOINT ["/bin/bash", "/shell-hook.sh"]
CMD ["calsync", "sync"]
