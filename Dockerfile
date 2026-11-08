FROM node:22-bookworm-slim AS frontend-build
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN npm ci --prefix frontend
COPY frontend ./frontend
COPY locales ./locales
ARG VITE_API_BASE_URL=/
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build --prefix frontend

FROM python:3.11 AS backend
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-dev --no-install-project
COPY backend ./backend
COPY locales ./locales
RUN cd backend && uv sync --frozen --no-dev
ENV PATH=/app/backend/.venv/bin:$PATH
WORKDIR /app/backend
EXPOSE 5001
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:application"]

FROM nginxinc/nginx-unprivileged:1.28-alpine AS frontend
COPY --from=frontend-build /src/frontend/dist /usr/share/nginx/html
COPY deploy/nginx/default.conf.template /etc/nginx/templates/default.conf.template
ENV BACKEND_URL=http://127.0.0.1:5001
ENV NGINX_ENVSUBST_FILTER=^BACKEND_URL$
EXPOSE 8080
