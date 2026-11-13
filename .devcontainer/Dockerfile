# Contributor environment on Debian testing → SETUP.md#dev-container.
# Same base as the CI debian-testing job: unpinned, it follows testing.
FROM debian:forky

# Keep in step with the local uv version → SETUP.md#dev-container.
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 \
    /uv /uvx /usr/local/bin/

# gcc, libc6-dev, and python3-dev build pyxattr, which PyPI ships as an sdist only.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates git sudo python3 python3-dev gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 dev \
    && useradd --uid 1000 --gid dev --create-home --shell /bin/bash dev \
    && echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev \
    && chmod 0440 /etc/sudoers.d/dev

USER dev
# The venv lives outside the workspace, so the host's .venv on the bind mount stays untouched.
ENV UV_PROJECT_ENVIRONMENT=/home/dev/.venv \
    UV_PYTHON_PREFERENCE=system \
    UV_LINK_MODE=copy
