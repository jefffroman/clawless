FROM public.ecr.aws/lambda/python:3.12

ARG SOCI_VERSION=0.13.0
ARG CRANE_VERSION=0.20.3

# tar/gzip needed to extract release tarballs. crane + soci both ship as
# single static Go binaries.
RUN dnf install -y tar gzip && dnf clean all

# crane: pulls/pushes OCI image layouts to/from registries. No daemon,
# no containerd. Replaces skopeo (which isn't in AL2023 repos).
RUN curl -fsSL \
      "https://github.com/google/go-containerregistry/releases/download/v${CRANE_VERSION}/go-containerregistry_Linux_arm64.tar.gz" \
      -o /tmp/crane.tar.gz \
    && tar xzf /tmp/crane.tar.gz -C /usr/local/bin/ crane \
    && chmod +x /usr/local/bin/crane \
    && rm /tmp/crane.tar.gz

# soci-snapshotter CLI. v0.13.0+ supports `convert --standalone` which
# produces a SOCI-v2 indexed image as a pure filesystem operation — no
# containerd, no ctr, no runc. We only need the `soci` binary (ignore
# soci-snapshotter-grpc, which is only for the runtime snapshotter).
RUN curl -fsSL \
      "https://github.com/awslabs/soci-snapshotter/releases/download/v${SOCI_VERSION}/soci-snapshotter-${SOCI_VERSION}-linux-arm64.tar.gz" \
      -o /tmp/soci.tar.gz \
    && tar xzf /tmp/soci.tar.gz -C /usr/local/bin/ soci \
    && chmod +x /usr/local/bin/soci \
    && rm /tmp/soci.tar.gz

RUN pip install --no-cache-dir boto3

COPY lambda/soci-index-builder/handler.py             ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
