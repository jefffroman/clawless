# clawless-searxng Lambda

Shared SearXNG service for all clawless agents. One function, not one per
agent — SearXNG is stateless request/response, so Lambda is a natural fit and
replaces per-agent SearXNG processes.

## How it works

- Container Lambda, base: `python:3.11-slim`
- [AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter)
  layer runs SearXNG's Flask webapp unmodified — the adapter translates Lambda
  invokes into local HTTP on `$PORT`
- Exposed via a Function URL (IAM auth). Gateway tasks call it as
  `https://<id>.lambda-url.<region>.on.aws/search?format=json&q=...`
- The openclaw searxng skill reads `SEARXNG_URL` from env; the Fargate task
  definition sets it to the function URL

## Cold start

SearXNG engine imports are heavy — budget 2–4s first hit. If that bites, add
provisioned concurrency = 1.

## Build

Built by Tofu via `null_resource.searxng_image`, same pattern as
`gateway_image`. Triggered on `Dockerfile` / `settings.yml` changes.
