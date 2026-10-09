FROM node:22-alpine AS frontend-build

WORKDIR /build/dashboard/frontend
COPY dashboard/frontend/package.json dashboard/frontend/package-lock.json ./
RUN npm ci
COPY dashboard/frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 1000 mike-trade \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin mike-trade

COPY pyproject.toml README.md ./
COPY mike_trade/ ./mike_trade/
COPY --from=frontend-build /build/dashboard/frontend/dist/ ./mike_trade/dashboard/frontend/dist/

RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && chown -R mike-trade:mike-trade /app

USER mike-trade

ENTRYPOINT ["mike-trade"]
CMD ["start"]
