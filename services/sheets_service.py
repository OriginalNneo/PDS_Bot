"""Google Sheets service for time tracking and SOA updates."""
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from pathlib import Path
from typing import Optional

# Google API scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Map display names to sheet keys (Andrew -> Andrew_Time)
USER_TO_SHEET = {
    "Andrew": "Andrew_Time",
    "Anna": "Anna_Time",
    "Audrey": "Audrey_Time",
    "Jonathan": "Jonathan_Time",
    "Nathaniel": "Nathaniel_Time",
}


class SheetsService:
    """Service for interacting with Google Sheets for time tracking and SOA."""

    def __init__(
        self,
        credentials_path: str,
        spreadsheet_id: str,
        user_sheets: Optional[dict] = None,
        soa_spreadsheet_id: Optional[str] = None,
    ):
        """Initialize the Sheets service.
        
        Args:
            credentials_path: Path to Google service account JSON
            spreadsheet_id: ID of the main Time Sheets spreadsheet
            user_sheets: Dict mapping Andrew_Time, Anna_Time, etc. to sheet gids
            soa_spreadsheet_id: ID of the separate SOA tracking spreadsheet
        """
        self.spreadsheet_id = spreadsheet_id
        self.user_sheets = user_sheets or {}
        self.soa_spreadsheet_id = soa_spreadsheet_id
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        self.client = gspread.authorize(creds)
        self.spreadsheet = self.client.open_by_key(spreadsheet_id)
        self.soa_spreadsheet = (
            self.client.open_by_key(soa_spreadsheet_id) if soa_spreadsheet_id else None
        )

    def _get_user_sheet(self, user_name: str) -> Optional[gspread.Worksheet]:
        """Get the worksheet for a user by their display name."""
        sheet_key = USER_TO_SHEET.get(user_name)
        if not sheet_key or sheet_key not in self.user_sheets:
            return None
        gid = int(self.user_sheets[sheet_key])
        try:
            return self.spreadsheet.get_worksheet_by_id(gid)
        except gspread.WorksheetNotFound:
            return None

    def record_meeting(
        self,
        user_name: str,
        time_start: datetime,
        time_end: datetime,
    ) -> float:
        """Record a meeting in the user's Time Sheet.
        
        Args:
            user_name: Name of the user (Andrew, Anna, etc.)
            time_start: Meeting start time
            time_end: Meeting end time
            
        Returns:
            Duration in minutes
        """
        duration_mins = (time_end - time_start).total_seconds() / 60
        worksheet = self._get_user_sheet(user_name)
        if not worksheet:
            raise ValueError(f"No sheet configured for user: {user_name}")
        
        # Ensure headers exist (if sheet is empty, add them first)
        try:
            values = worksheet.get_all_values()
            if not values or not any(str(v or "").strip().lower().startswith("date") for v in (values[0] if values else [])):
                worksheet.update("A1:E1", [["Date", "Time Start", "Time End", "Duration (mins)", "Notes"]])
        except Exception:
            pass
        
        date_str = time_start.strftime("%d/%m/%Y")
        time_start_str = time_start.strftime("%H:%M")
        time_end_str = time_end.strftime("%H:%M")
        
        row = [date_str, time_start_str, time_end_str, round(duration_mins, 2), "Meeting"]
        worksheet.append_row(row)
        
        return duration_mins

    def record_meeting_for_all(self, time_start: datetime, time_end: datetime) -> float:
        """Record meeting duration for ALL members (Andrew, Anna, Audrey, Jonathan, Nathaniel).
        
        When a meeting ends, everyone gets the same duration credited to their time sheet.
        
        Args:
            time_start: Meeting start time
            time_end: Meeting end time
            
        Returns:
            Duration in minutes
        """
        duration_mins = (time_end - time_start).total_seconds() / 60
        for user_name in USER_TO_SHEET:
            try:
                self.record_meeting(user_name, time_start, time_end)
            except ValueError:
                pass  # Skip if no sheet configured for user
        return duration_mins

    def add_manual_time(self, user_name: str, minutes: float) -> float:
        """Manually add time for a user on today's date.
        
        Args:
            user_name: Name of the user (Andrew, Anna, Audrey, etc.)
            minutes: Number of minutes to add
            
        Returns:
            The minutes added
        """
        worksheet = self._get_user_sheet(user_name)
        if not worksheet:
            raise ValueError(f"No sheet configured for user: {user_name}")
        
        # Ensure headers exist
        try:
            values = worksheet.get_all_values()
            if not values or not any(str(v or "").strip().lower().startswith("date") for v in (values[0] if values else [])):
                worksheet.update("A1:E1", [["Date", "Time Start", "Time End", "Duration (mins)", "Notes"]])
        except Exception:
            pass
        
        now = datetime.now()
        time_end = now
        time_start = datetime.fromtimestamp(now.timestamp() - minutes * 60)
        date_str = now.strftime("%d/%m/%Y")
        time_start_str = time_start.strftime("%H:%M")
        time_end_str = time_end.strftime("%H:%M")
        
        row = [date_str, time_start_str, time_end_str, round(minutes, 2), "Manual update"]
        worksheet.append_row(row)
        
        return minutes

    def _normalize_date(self, d: str) -> str:
        """Normalize date to DD/MM/YYYY for comparison."""
        if not d:
            return ""
        d = str(d).strip()
        # Handle 2/2/2026 -> 02/02/2026
        parts = d.replace("-", "/").split("/")
        if len(parts) == 3:
            try:
                day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
                return f"{day:02d}/{month:02d}/{year}"
            except (ValueError, IndexError):
                pass
        return d

    def _looks_like_date(self, s: str) -> bool:
        """Check if string looks like a date (DD/MM/YYYY or similar)."""
        if not s or len(s) < 8:
            return False
        parts = s.replace("-", "/").replace(".", "/").split("/")
        if len(parts) != 3:
            return False
        try:
            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
            return 1 <= d <= 31 and 1 <= m <= 12 and 1900 <= y <= 2100
        except (ValueError, IndexError):
            return False

    def get_user_times_for_date(self, date_str: Optional[str] = None) -> dict:
        """Get total time per user for a given date from each user's sheet.
        
        Uses flexible column matching - works with various header names and structures.
        
        Args:
            date_str: Date in DD/MM/YYYY format. Defaults to today.
            
        Returns:
            Dict mapping user name to total minutes
        """
        if date_str is None:
            date_str = datetime.now().strftime("%d/%m/%Y")
        target_date = self._normalize_date(date_str)
        
        user_times = {}
        for user_name in USER_TO_SHEET:
            worksheet = self._get_user_sheet(user_name)
            if not worksheet:
                user_times[user_name] = 0
                continue
            
            total = 0
            try:
                values = worksheet.get_all_values()
            except Exception:
                values = []
            
            if not values:
                user_times[user_name] = 0
                continue
            
            headers = [str(h or "").strip().lower() for h in values[0]]
            # Check if row 0 is headers (contains "date") or data (first col looks like date DD/MM/YYYY)
            first_cell = str(values[0][0] or "").strip()
            has_header_row = any("date" in h for h in headers) or not self._looks_like_date(first_cell)
            data_start = 1 if has_header_row else 0
            
            # Find column indices: date (col 0 typical), duration (col 3 typical)
            date_col = 0
            duration_col = 3
            if has_header_row:
                for i, h in enumerate(headers):
                    if h and "date" in h:
                        date_col = i
                    if h and ("duration" in h or "mins" in h or "min" in h):
                        duration_col = i
            
            for row in values[data_start:]:
                if len(row) <= max(date_col, duration_col):
                    continue
                record_date = self._normalize_date(row[date_col] if date_col < len(row) else "")
                if record_date != target_date:
                    continue
                try:
                    dur_val = row[duration_col] if duration_col < len(row) else 0
                    total += float(dur_val) if dur_val else 0
                except (ValueError, TypeError):
                    pass
            user_times[user_name] = total
        
        return user_times

    def get_soa_budget_spent(self) -> float:
        """Get Budget Spent from SOA sheet (sum of Amount column)."""
        if not self.soa_spreadsheet:
            return 0.0
        try:
            worksheet = self.soa_spreadsheet.sheet1
            records = worksheet.get_all_records()
            total = 0.0
            for record in records:
                # Support both "Amount" (new format) and "Total" (legacy)
                val = record.get("Amount", record.get("amount", record.get("Total", record.get("total", 0))))
                try:
                    total += float(val)
                except (ValueError, TypeError):
                    pass
            return total
        except Exception:
            return 0.0

    def get_summary(self, date_str: Optional[str] = None) -> dict:
        """Get summary of times for Andrew, Anna, Audrey, Jonathan, Nathaniel.
        
        Budget Spent = sum of Total column from SOA sheet.
        Budget Left = budget_total - budget_spent.
        
        Args:
            date_str: Date in DD/MM/YYYY. Defaults to today.
            
        Returns:
            Dict with user times, budget_spent, budget_left
        """
        if date_str is None:
            date_str = datetime.now().strftime("%d/%m/%Y")
        user_times = self.get_user_times_for_date(date_str)
        
        # Ensure all expected users are in the result
        expected_users = ["Andrew", "Anna", "Audrey", "Jonathan", "Nathaniel"]
        result = {user: user_times.get(user, 0) for user in expected_users}
        
        # Budget Spent = sum of Total from SOA sheet
        budget_spent = self.get_soa_budget_spent()
        
        # Get budget total from config
        try:
            from config import load_config
            config = load_config()
            budget_total = float(config.get("budget_total", 0) or 0)
        except Exception:
            budget_total = 0
        
        result["budget_spent"] = budget_spent
        result["budget_left"] = budget_total - budget_spent if budget_total else 0
        result["budget_total"] = budget_total

        return result

    def update_soa(self, data: list[dict]) -> bool:
        """Update the SOA (Statement of Account) tracking sheet.
        
        Columns: Date bought | Item | Amount
        Each row: date | item name | amount (line total)
        
        Args:
            data: List of dicts with keys: Date, Item, Price, Qty, Total
            
        Returns:
            True if successful
        """
        if self.soa_spreadsheet:
            worksheet = self.soa_spreadsheet.sheet1  # gid=0 is first sheet
        else:
            try:
                worksheet = self.spreadsheet.worksheet("SOA")
            except gspread.WorksheetNotFound:
                worksheet = self.spreadsheet.add_worksheet(title="SOA", rows=1000, cols=3)
                worksheet.update("A1:C1", [["Date bought", "Item", "Amount"]])
        
        rows = []
        for item in data:
            total_val = item.get("Total", 0)
            try:
                total_val = float(total_val) if total_val else 0
            except (ValueError, TypeError):
                total_val = 0
            rows.append([
                item.get("Date", ""),
                item.get("Item", ""),
                total_val,
            ])
        
        if rows:
            worksheet.append_rows(rows, value_input_option="USER_ENTERED")
        
        return True
