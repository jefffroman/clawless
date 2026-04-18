FROM public.ecr.aws/lambda/python:3.12

ARG SOCI_VERSION=0.10.0

# Containerd + runc (no Docker — Lambda has no daemon anyway). tar/gzip
# needed to extract the soci release tarball. boto3 ships with the Python
# base image but we pin anyway to avoid surprises.
RUN dnf install -y containerd runc tar gzip findutils && dnf clean all

# Lambda's rootfs is read-only except /tmp. containerd 2.x has a hardcoded
# /run/containerd path that --state doesn't fully override (some plugins
# still write there). Pre-create it as a symlink into /tmp so containerd's
# idempotent MkdirAll succeeds at runtime.
RUN ln -s /tmp/containerd /run/containerd

# soci-snapshotter CLI. The release tarball layout varies across versions,
# so extract + find rather than assuming a path.
RUN curl -fsSL \
      "https://github.com/awslabs/soci-snapshotter/releases/download/v${SOCI_VERSION}/soci-snapshotter-${SOCI_VERSION}-linux-arm64.tar.gz" \
      -o /tmp/soci.tar.gz \
    && mkdir -p /tmp/soci-extract \
    && tar xzf /tmp/soci.tar.gz -C /tmp/soci-extract \
    && find /tmp/soci-extract -type f -executable -name 'soci*' \
         -exec mv {} /usr/local/bin/ \; \
    && chmod +x /usr/local/bin/soci* \
    && rm -rf /tmp/soci.tar.gz /tmp/soci-extract

RUN pip install --no-cache-dir boto3

COPY lambda/soci-index-builder/handler.py             ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
