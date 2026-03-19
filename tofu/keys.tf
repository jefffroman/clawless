# Lightsail key pair used by Ansible for SSH provisioning.
resource "aws_lightsail_key_pair" "ansible" {
  name       = "clawless-ansible"
  public_key = var.provisioner_public_key
}
