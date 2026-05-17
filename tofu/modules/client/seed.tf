# ── Workspace seed files ─────────────────────────────────────────────────────
# First-boot scaffolding for an agent's S3 workspace. Templates are rendered
# at apply time with agent/client identity and uploaded to the backup bucket
# at s3://BACKUP_BUCKET/agents/{slug}/workspace/memory/...
#
# This is the path clawless-gateway's memory module reads on every retrieval
# (see app/config.py: memory_source_dir = ${WORKSPACE_DIR}/memory).
#
# Persona model: the normalized agent name selects a persona directory under
# seed/personas/<persona_key>/. SOUL.md is persona-defined and mandatory —
# there is no generic fallback; an unknown persona fails `plan` early (see the
# precondition below). MEMORY.md/USER.md are generic scaffolds a persona MAY
# override by shipping a same-named .tftpl in its directory.
#
# These are write-once: `ignore_changes = [content, ...]` blocks future
# applies from clobbering what the agent has edited in-workspace. Editing a
# template and re-applying will NOT overwrite an existing object — the
# resource is effectively create-only, so persona only applies at creation.
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

  seed_prefix = "agents/${var.agent_slug}/workspace/memory"

  # Persona resolution: normalize the effective agent name to a directory key
  # (lowercase; anything outside [a-z0-9_-] becomes '-'). e.g. "Pixel Pal"
  # → "pixel-pal", "gamer" → "gamer".
  agent_name_effective = var.agent_name != "" ? var.agent_name : local.slug_safe
  persona_key          = replace(lower(trimspace(local.agent_name_effective)), "/[^a-z0-9_-]/", "-")
  persona_dir          = "${path.module}/seed/personas/${local.persona_key}"

  # Personas that actually exist (defined by shipping a SOUL.md.tftpl) — used
  # only to make the unknown-persona error message actionable.
  available_personas = sort([
    for p in fileset("${path.module}/seed/personas", "*/SOUL.md.tftpl") : dirname(p)
  ])

  # Generic scaffolds (a persona may override any of these by shipping a
  # same-named .tftpl in its directory). SOUL.md is intentionally NOT here —
  # it is persona-only and has no generic default.
  seed_md_files = {
    "MEMORY.md" = "MEMORY.md.tftpl"
    "USER.md"   = "USER.md.tftpl"
  }

  seed_md_resolved = merge(
    {
      for fname, tpl in local.seed_md_files :
      fname => fileexists("${local.persona_dir}/${tpl}")
      ? "${local.persona_dir}/${tpl}"
      : "${path.module}/seed/${tpl}"
    },
    # Persona-defined, mandatory, no fallback. The precondition below turns a
    # missing one into a clean early failure rather than a templatefile error.
    { "SOUL.md" = "${local.persona_dir}/SOUL.md.tftpl" },
  )
}

resource "aws_s3_object" "seed_md" {
  for_each = local.seed_md_resolved

  bucket  = var.backup_bucket
  key     = "${local.seed_prefix}/${each.key}"
  content = templatefile(each.value, local.seed_vars)

  lifecycle {
    # OpenTofu evaluates preconditions before the resource's own config, so an
    # unknown persona aborts `plan` here with an actionable message instead of
    # a raw "no file exists" templatefile error.
    precondition {
      condition     = fileexists("${local.persona_dir}/SOUL.md.tftpl")
      error_message = "Unknown persona '${local.persona_key}' for agent '${local.agent_name_effective}'. Expected ${local.persona_dir}/SOUL.md.tftpl. Available personas: ${join(", ", local.available_personas)}."
    }
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
