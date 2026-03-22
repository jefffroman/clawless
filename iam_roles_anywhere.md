To secure a Lightsail instance using IAM Roles Anywhere and AWS Systems Manager (SSM), follow these sequential steps to replace static keys with certificate-based temporary credentials.
 1. Establish the Certificate Authority (CA)
    Generate Root CA: Create a self-signed Root CA (using OpenSSL) to act as your Trust Anchor.
Issue Client Certificate: Generate a unique private key and X.509 certificate for your Lightsail instance, signed by your Root CA.
 2. Configure AWS Infrastructure
    Create Trust Anchor: Upload your Root CA certificate to the IAM Roles Anywhere console.
    Define IAM Role: Create a role with the AmazonSSMManagedInstanceCore policy. Edit the Trust Relationship to allow rolesanywhere.amazonaws.com to assume the role.
Create Profile: In the Roles Anywhere console, create a Profile that links your Trust Anchor to the IAM role you created.
 3. Setup the Lightsail Instance
    Install Credential Helper: Download and install the AWS Signing Helper binary on your instance.
    Configure AWS CLI: Add a profile to ~/.aws/config using the credential_process setting to call the signing helper with your client certificate and private key.
 4. Register with Systems Manager
    Initial Registration: Use the SSM Agent to register the instance as a hybrid node using a one-time activation code/ID.
Identity Switch: Once registered, configure the SSM Agent to use the credentials provided by your credential_process for all subsequent "re-registrations" and check-ins.
 5. Automated Refresh (The "Lever")
    Proactive Refresh: The AWS SDK/CLI will automatically trigger the credential_process script approximately 5 minutes before expiry whenever an API call is made, ensuring continuous access without manual intervention.
