# ── Workspace seed files ─────────────────────────────────────────────────────
# First-boot scaffolding for an agent's S3 workspace. Templates are rendered
# at apply time with agent/client identity and uploaded to the backup bucket
# at s3://BACKUP_BUCKET/agents/{slug}/workspace/.openclaw/workspace/...
#
# These are write-once: `ignore_changes = [content, content_base64, etag,
# source_hash]` blocks future applies from clobbering what the agent has
# edited in-workspace. Editing a template and re-applying will NOT overwrite
# an existing object — the resource is effectively create-only.
#
# Removing an agent destroys the seed objects. The lifecycle Lambda archives
# the workspace prefix before tofu destroy runs, so nothing is lost.

locals {
  seed_vars = {
    agent_name    = var.agent_name != "" ? var.agent_name : local.slug_safe
    client_name   = var.client_name
    agent_style   = var.agent_style
    agent_channel = var.agent_channel
  }

  seed_prefix = "agents/${var.agent_slug}/workspace/.openclaw/workspace"

  seed_md_files = {
    "MEMORY.md"    = "${path.module}/seed/MEMORY.md.tftpl"
    "IDENTITY.md"  = "${path.module}/seed/IDENTITY.md.tftpl"
    "USER.md"      = "${path.module}/seed/USER.md.tftpl"
    "AGENTS.md"    = "${path.module}/seed/AGENTS.md.tftpl"
    "BOOTSTRAP.md" = "${path.module}/seed/BOOTSTRAP.md.tftpl"
    "SOUL.md"      = "${path.module}/seed/SOUL.md.tftpl"
    "TOOLS.md"     = "${path.module}/seed/TOOLS.md.tftpl"
    "HEARTBEAT.md" = "${path.module}/seed/HEARTBEAT.md.tftpl"
  }
}

resource "aws_s3_object" "seed_md" {
  for_each = local.seed_md_files

  bucket  = var.backup_bucket
  key     = "${local.seed_prefix}/${each.key}"
  content = templatefile(each.value, local.seed_vars)

  lifecycle {
    ignore_changes = [content, content_base64, etag, source_hash, metadata, tags, tags_all]
  }

  tags = var.tags
}

resource "aws_s3_object" "seed_gitignore" {
  bucket = var.backup_bucket
  key    = "${local.seed_prefix}/.gitignore"
  source = "${path.module}/seed/workspace.gitignore"
  etag   = filemd5("${path.module}/seed/workspace.gitignore")

  lifecycle {
    ignore_changes = [content, content_base64, etag, source_hash, metadata, tags, tags_all]
  }

  tags = var.tags
}
