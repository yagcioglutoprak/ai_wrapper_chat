"""File storage abstraction.

Uses Azure Blob Storage when AZURE_STORAGE_CONNECTION_STRING is set,
otherwise falls back to local filesystem.
"""

import os
import uuid


class LocalStorage:
    """Store files on local filesystem."""

    def __init__(self, upload_folder):
        self.upload_folder = upload_folder
        os.makedirs(upload_folder, exist_ok=True)

    def upload(self, file_data, filename, content_type="application/octet-stream"):
        safe_name = f"{uuid.uuid4().hex}_{filename}"
        path = os.path.join(self.upload_folder, safe_name)
        with open(path, "wb") as f:
            f.write(file_data)
        return safe_name, len(file_data)

    def download(self, blob_name):
        path = os.path.join(self.upload_folder, blob_name)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def delete(self, blob_name):
        path = os.path.join(self.upload_folder, blob_name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def get_url(self, blob_name):
        return f"/files/serve/{blob_name}"


class AzureBlobStorage:
    """Store files in Azure Blob Storage."""

    def __init__(self, connection_string, container_name):
        from azure.storage.blob import BlobServiceClient

        self.blob_service = BlobServiceClient.from_connection_string(
            connection_string
        )
        self.container_name = container_name
        # Create container if it doesn't exist
        try:
            self.blob_service.create_container(container_name)
        except Exception:
            pass  # Container already exists

    def upload(self, file_data, filename, content_type="application/octet-stream"):
        from azure.storage.blob import ContentSettings

        blob_name = f"{uuid.uuid4().hex}_{filename}"
        blob_client = self.blob_service.get_blob_client(
            container=self.container_name, blob=blob_name
        )
        blob_client.upload_blob(
            file_data,
            content_settings=ContentSettings(content_type=content_type),
            overwrite=True,
        )
        return blob_name, len(file_data)

    def download(self, blob_name):
        blob_client = self.blob_service.get_blob_client(
            container=self.container_name, blob=blob_name
        )
        try:
            return blob_client.download_blob().readall()
        except Exception:
            return None

    def delete(self, blob_name):
        blob_client = self.blob_service.get_blob_client(
            container=self.container_name, blob=blob_name
        )
        try:
            blob_client.delete_blob()
            return True
        except Exception:
            return False

    def get_url(self, blob_name):
        blob_client = self.blob_service.get_blob_client(
            container=self.container_name, blob=blob_name
        )
        return blob_client.url


def get_storage(app):
    """Factory: return Azure or Local storage based on config."""
    conn_string = app.config.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if conn_string:
        return AzureBlobStorage(
            conn_string, app.config["AZURE_STORAGE_CONTAINER"]
        )
    return LocalStorage(app.config["UPLOAD_FOLDER"])
