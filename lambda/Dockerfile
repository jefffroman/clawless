FROM public.ecr.aws/lambda/python:3.12

ARG TOFU_VERSION=1.10.0

RUN dnf install -y git unzip && dnf clean all

# Install OpenTofu
RUN curl -fsSL \
    "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_arm64.zip" \
    -o /tmp/tofu.zip \
    && unzip /tmp/tofu.zip tofu -d /usr/local/bin/ \
    && rm /tmp/tofu.zip

# Pre-bake provider plugins so tofu init is fast at runtime.
# Only needs rebuilding when provider versions change (rare).
ENV TF_PLUGIN_CACHE_DIR=/opt/tofu-plugin-cache
RUN mkdir -p /opt/tofu-plugin-cache /opt/tofu-init
COPY tofu/providers.tf tofu/.terraform.lock.hcl /opt/tofu-init/
RUN cd /opt/tofu-init && tofu init -backend=false

COPY lambda/handler.py ${LAMBDA_TASK_ROOT}/
CMD ["handler.lambda_handler"]
