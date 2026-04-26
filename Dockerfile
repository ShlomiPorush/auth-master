FROM node:20-alpine AS css
WORKDIR /build
COPY services/styles/package.json ./auth/styles/
RUN cd auth/styles && npm install
COPY services/styles/ ./auth/styles/
COPY services/static/public/ ./auth/static/public/
RUN cd auth/styles && npm run build

FROM python:3.11-slim
WORKDIR /app
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY services/requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY services/app ./app
COPY services/static ./static
COPY --from=css /build/auth/static/public/css/tailwind.css ./static/public/css/tailwind.css

RUN mkdir -p /data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health').read()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
