"""Google Drive service for document uploads with DD/MM/YYYY folder structure."""
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from pathlib import Path
from datetime import datetime
from typing import Optional

SCOPES = ["https://www.googleapis.com/auth/drive"]


class DriveService:
    """Service for uploading documents to Google Drive with date-based folders."""

    def __init__(self, credentials_path: str, root_folder_id: str):
        """Initialize the Drive service.
        
        Args:
            credentials_path: Path to Google service account JSON
            root_folder_id: ID of the root folder where date folders will be created
        """
        self.root_folder_id = root_folder_id
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        self.service = build("drive", "v3", credentials=creds)

    def _get_folder_name(self, date: Optional[datetime] = None) -> str:
        """Get folder name in DD/MM/YYYY format."""
        if date is None:
            date = datetime.now()
        return date.strftime("%d/%m/%Y")

    def _find_folder_by_name(self, folder_name: str, parent_id: str) -> Optional[str]:
        """Find a folder by name within a parent folder.
        
        Returns:
            Folder ID if found, None otherwise
        """
        query = (
            f"name = '{folder_name}' and "
            f"'{parent_id}' in parents and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        )
        results = (
            self.service.files()
            .list(q=query, spaces="drive", fields="files(id, name)")
            .execute()
        )
        files = results.get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self, folder_name: str, parent_id: str) -> str:
        """Create a folder and return its ID."""
        file_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = self.service.files().create(body=file_metadata, fields="id").execute()
        return folder["id"]

    def _get_or_create_date_folder(self, date: Optional[datetime] = None) -> str:
        """Get or create the folder for the given date (DD/MM/YYYY).
        
        Returns:
            Folder ID
        """
        folder_name = self._get_folder_name(date)
        folder_id = self._find_folder_by_name(folder_name, self.root_folder_id)
        if folder_id:
            return folder_id
        return self._create_folder(folder_name, self.root_folder_id)

    def upload_file(
        self,
        file_path: str,
        date: Optional[datetime] = None,
        file_name: Optional[str] = None,
    ) -> str:
        """Upload a file to the appropriate DD/MM/YYYY folder.
        
        Args:
            file_path: Local path to the file
            date: Date for folder. Defaults to today.
            file_name: Optional name for the file in Drive. Defaults to local filename.
            
        Returns:
            Google Drive file ID of the uploaded file
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        folder_id = self._get_or_create_date_folder(date)
        
        mime_types = {
            ".pdf": "application/pdf",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        mime_type = mime_types.get(path.suffix.lower(), "application/octet-stream")
        
        name = file_name or path.name
        file_metadata = {
            "name": name,
            "parents": [folder_id],
        }
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        
        file = (
            self.service.files()
            .create(body=file_metadata, media_body=media, fields="id, webViewLink")
            .execute()
        )
        
        return file.get("id", "")
