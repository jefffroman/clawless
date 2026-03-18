# Lightsail key pair used by Ansible for SSH provisioning.
# The private key lives at ~/.ssh/clawless_ansible (never committed).
# Override provisioner_public_key_path in tfvars if using a different key.
resource "aws_lightsail_key_pair" "ansible" {
  name       = "clawless-ansible"
  public_key = file(var.provisioner_public_key_path)
}
