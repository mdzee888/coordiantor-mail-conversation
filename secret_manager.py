from google.cloud import secretmanager

class SecretManager:
    def __init__(self, project_id):
        self.project_id = project_id

    def get_secrets(self, secret_id):
        """Retrieves the latest version of the specified secret."""
        return self.access_secret_version(self.project_id, secret_id)

    def access_secret_version(self, project_id, secret_id, version_id='latest'):
        # Create the Secret Manager client.
        client = secretmanager.SecretManagerServiceClient()

        # Build the resource name of the secret version.
        name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"

        # Access the secret version.
        response = client.access_secret_version(request={"name": name})

        # Decode the payload
        payload = response.payload.data.decode("UTF-8")
        return payload
