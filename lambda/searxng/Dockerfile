FROM python:3.11-slim

# AWS Lambda Web Adapter — runs any HTTP server as a Lambda function.
# Extension at /opt/extensions is auto-detected by the Lambda runtime.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git build-essential python3-dev libxml2-dev libxslt1-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

ARG SEARXNG_REF=master
RUN git clone --depth 1 --branch "${SEARXNG_REF}" https://github.com/searxng/searxng.git /opt/searxng/searxng-src \
    && pip install --no-cache-dir -r /opt/searxng/searxng-src/requirements.txt \
    && apt-get purge -y --auto-remove build-essential python3-dev \
    && rm -rf /root/.cache/pip

COPY lambda/searxng/settings.yml /etc/searxng/settings.yml
RUN SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '/+' '_-') \
    && sed -i "s|__SECRET_KEY__|${SECRET}|" /etc/searxng/settings.yml

ENV SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml \
    PYTHONPATH=/opt/searxng/searxng-src \
    PORT=8080 \
    AWS_LWA_INVOKE_MODE=buffered \
    AWS_LWA_READINESS_CHECK_PATH=/healthz

WORKDIR /opt/searxng/searxng-src
CMD ["python", "searx/webapp.py"]
