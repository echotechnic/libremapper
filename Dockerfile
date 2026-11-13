FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LIBREMAPPER_CONFIG=/config/libremapper.yaml \
    LIBREMAPPER_CONFIG_ROOT=/config \
    LIBREMAPPER_DATA_ROOT=/data/libremapper \
    LIBREMAPPER_LOCK_PATH=/data/libremapper/action.lock \
    LIBREMAPPER_STATE_PATH=/data/libremapper/state.sqlite3 \
    LIBREMAPPER_DROP_PRIVILEGES=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE COPYRIGHT.md ./
COPY LICENSES ./LICENSES
COPY src ./src
COPY composer.json /usr/share/libremapper-plugin/composer.json
COPY plugin/src /usr/share/libremapper-plugin/plugin/src
COPY plugin/resources /usr/share/libremapper-plugin/plugin/resources
COPY plugin/routes /usr/share/libremapper-plugin/plugin/routes
COPY skills /usr/share/libremapper-plugin/skills
COPY compose.plugin.yaml /usr/local/share/libremapper/compose.libremapper.yaml
COPY --chmod=755 scripts/install-or-update /usr/local/share/libremapper/install-or-update
RUN apt-get update \
    && apt-get install --no-install-recommends -y git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir .

VOLUME ["/config", "/data/libremapper"]
CMD ["libremapper-api"]
