FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e

COPY --from=ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 /uv /usr/local/bin/uv

# Built from this checkout, with every dependency installed from uv.lock and checked against
# its recorded hashes. Build with: docker build --build-arg VERSION=$(git describe --tags) .
ARG VERSION=0.0.0
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN SETUPTOOLS_SCM_PRETEND_VERSION="${VERSION}" \
    uv sync --locked --no-dev --no-editable --compile-bytecode \
    && rm /usr/local/bin/uv \
    && install -d -o 10001 -g 10001 /config

LABEL org.opencontainers.image.source="https://github.com/srujanmalakpata/matrix-mcp"
ENV HOME=/config \
    XDG_CONFIG_HOME=/config \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_SHOW_SERVER_BANNER=false \
    FASTMCP_TRANSPORT=stdio

# Run with only the config volume mounted, for example:
#   docker run -i --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
#     -v matrix-mcp-config:/config --network <hub network> IMAGE serve
USER 10001:10001
WORKDIR /config
ENTRYPOINT ["matrix-mcp"]
CMD ["serve"]
