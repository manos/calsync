# syntax=docker/dockerfile:1

# The runtime is a plain slim CPython with PyPI wheels rather than the pixi/conda env
# used for local development: same dependency set, a third of the size (the conda env
# carries its own libpython, libicudata, tcl/tk, headers and a full bin/). pyproject.toml
# stays the single source of truth -- requirements.lock is compiled from it. Regenerate
# the lock after any dependency change with the command recorded in its own header:
#   uv pip compile pyproject.toml --universal --python-version 3.13 --generate-hashes \
#       -o requirements.lock
ARG PYTHON_TAG=3.13-slim-bookworm
ARG UV_TAG=0.12.3

FROM ghcr.io/astral-sh/uv:${UV_TAG} AS uv

FROM python:${PYTHON_TAG} AS build

COPY --from=uv /uv /usr/local/bin/uv
# PYTHONDONTWRITEBYTECODE keeps every python run in this stage -- notably the selfchecks
# below -- from re-littering the tree with __pycache__ after it has been cleaned out.
ENV UV_LINK_MODE=copy UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never VIRTUAL_ENV=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1

# binutils supplies strip, used below. Build stage only; never lands in the runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies come only from the hash-pinned lock: --require-hashes refuses to resolve
# anything at build time, so the image is reproducible from the committed file alone.
COPY requirements.lock ./
RUN uv venv "$VIRTUAL_ENV" \
    && uv pip install --require-hashes --requirement requirements.lock

# calsync itself is the one thing not in the lock (it is the project, not a dependency).
# --build-constraints pins the hatchling that builds the wheel, so this step resolves
# nothing at build time either, and --no-deps keeps the lock the only source of deps.
COPY requirements-build.lock ./
COPY pyproject.toml ./
COPY src ./src
RUN uv pip install --no-deps --build-constraints requirements-build.lock .

# google-api-python-client ships a static discovery document for every Google API:
# ~100MB across 598 files, of which calsync only ever asks for calendar v3.
# googleapiclient.discovery_cache.get_static_doc() opens "<service>.<version>.json" by
# name and consults no index, so dropping the rest is safe. Assert the wanted document
# exists first -- an upstream rename would otherwise silently produce an image with no
# calendar discovery at all, and that failure would only surface against live Google.
RUN DOCS="$VIRTUAL_ENV/lib/python3.13/site-packages/googleapiclient/discovery_cache/documents" \
    && test -f "$DOCS/calendar.v3.json" \
    && find "$DOCS" -type f -name '*.json' ! -name 'calendar.v3.json' -delete

# caldav pulls niquests, which pulls urllib3-future; requests pulls stock urllib3.
# Both claim site-packages/urllib3, and urllib3-future ships a .pth that repairs the
# clash by overwriting that directory on interpreter start -- but only when it is
# writable. The runtime stage is non-root over a root-owned env, so that repair can
# never run there and a half-merged tree would be frozen into the image. Settle it
# here, while the tree is still writable, and fail the build if imports stay broken.
COPY scripts/selfcheck.py /selfcheck.py
RUN "$VIRTUAL_ENV/bin/python" /selfcheck.py

# Drop what a runtime never reads:
#  - bytecode caches: site-packages is root-owned and the process is not, so nothing
#    could refresh a stale .pyc anyway. calsync is a long-lived loop; paying the compile
#    once per start is invisible next to a 15m sync interval.
#  - build-time sources for compiling against these packages: C headers, Cython
#    declarations, type stubs. Only compilers and type checkers open them.
#  - the test suites icalendar and recurring-ical-events ship inside their packages.
#    Named explicitly rather than matched by a glob, so a package that genuinely imports
#    something called "tests" cannot be caught by accident.
#  - debug symbols in compiled extensions. cryptography's rust module and pyyaml's
#    _yaml arrive unstripped from PyPI; the dynamic linker needs .dynsym, which
#    --strip-unneeded keeps. The selfcheck below loads every extension afterwards.
RUN SP="$VIRTUAL_ENV/lib/python3.13/site-packages" \
    && find "$VIRTUAL_ENV" -type d -name '__pycache__' -prune -exec rm -rf {} + \
    && find "$VIRTUAL_ENV" -type f \
        \( -name '*.pyc' -o -name '*.pyo' -o -name '*.h' -o -name '*.pxd' \
           -o -name '*.pyx' -o -name '*.pyi' -o -name '*.a' \) -delete \
    && rm -rf "$SP/icalendar/tests" "$SP/recurring_ical_events/test" \
    && find "$VIRTUAL_ENV" -type f -name '*.so' -exec strip --strip-unneeded {} +

# Re-run the gate over the stripped tree: nothing calsync needs -- imports, the calendar
# discovery document, cryptography, lxml, every compiled extension -- may have gone with
# it. Then assert the strip actually held, since a python run that wrote bytecode back
# would silently return the ~10MB the purge above just reclaimed.
RUN "$VIRTUAL_ENV/bin/python" /selfcheck.py \
    && test -z "$(find "$VIRTUAL_ENV" \( -name '__pycache__' -o -name '*.pyc' \) -print -quit)"

FROM python:${PYTHON_TAG} AS runtime

# ca-certificates and tzdata already come with the base image; the user does not.
# Nothing else is deleted here on purpose: removing the base image's pip, idlelib or C
# headers in this stage would only stack whiteouts on top of the layers that still carry
# them, costing a layer and reclaiming nothing.
RUN useradd --system --uid 10001 --create-home calsync

WORKDIR /app
COPY --from=build /opt/venv /opt/venv

USER calsync
# The venv on PATH replaces the pixi shell-hook the previous image needed as an
# entrypoint, so `docker run <image> calsync ...` keeps working unchanged.
ENV PATH="/opt/venv/bin:$PATH" \
    SYNC_INTERVAL=15m \
    LOG_LEVEL=info \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=2 \
    CMD ["calsync", "healthcheck"]

CMD ["calsync", "sync"]
