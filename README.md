# Teck Ghee Block Finder - Telegram Bot

A Telegram bot for meeting time tracking, document management, and receipt extraction. **Only responds in groups when mentioned via @BotName.**

## Features

- **Meeting Time Tracking**: `/start_meeting` and `/end_meeting` to track meeting duration and update Google Time Sheets
- **Summary**: `/summary` for a brief overview of times and budget
- **Document Upload**: PDF and Word documents uploaded to Google Drive in DD/MM/YYYY folders
- **Receipt Extraction**: `/pdf` + PDF attachment to extract amounts and update SOA (Statement of Account) tracking sheet

## Setup

### 1. Create Virtual Environment

```bash
cd "Teck Ghee Block Finder"
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure API Keys

Edit `api_keys.json` (or copy from `api_keys.json.example`):

```json
{
  "bot_key": "YOUR_TELEGRAM_BOT_TOKEN",
  "google_credentials": "path/to/credentials.json",
  "spreadsheet_id": "YOUR_TIME_SHEETS_SPREADSHEET_ID",
  "drive_folder_id": "YOUR_GOOGLE_DRIVE_ROOT_FOLDER_ID",
  "user_sheets": {
    "Andrew_Time": "0",
    "Anna_Time": "1112519352",
    "Audrey_Time": "84938743",
    "Jonathan_Time": "1853190125",
    "Nathaniel_Time": "1788508635"
  },
  "soa_spreadsheet_id": "YOUR_SOA_TRACKING_SPREADSHEET_ID",
  "budget_total": "480"
}
```

- **bot_key**: Get from [@BotFather](https://t.me/BotFather) on Telegram
- **google_credentials**: Path to Google service account JSON (see below)
- **spreadsheet_id**: Main Time Sheets spreadsheet ID (from URL)
- **user_sheets**: Map each user's sheet (Andrew_Time, Anna_Time, etc.) to its gid (from URL `#gid=123`)
- **soa_spreadsheet_id**: Separate SOA tracking spreadsheet ID
- **drive_folder_id**: Root folder ID for document uploads
- **budget_total**: Total budget (e.g., minutes per day)

### 3. Google Cloud Setup (Required for Sheets & Drive)

**Important:** You need a **Service Account JSON file**, not an API key.

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create or select a project
3. Enable **Google Sheets API** and **Google Drive API** (APIs & Services → Library)
4. Create a Service Account:
   - APIs & Services → Credentials → Create Credentials → Service Account
   - Give it a name (e.g. "teck-ghee-bot") → Create
5. Create a key for the service account:
   - Click the service account → Keys tab → Add Key → Create new key → JSON
   - Download the JSON file
6. Save the downloaded file as `credentials.json` in the project folder
7. **Share your Google Sheet and Drive folder** with the service account email (e.g. `something@project.iam.gserviceaccount.com`):
   - Open your Sheet → Share → add the service account email as Editor
   - Open your Drive folder → Share → add the service account email as Editor

### 4. Run the Bot

```bash
python bot.py
```

### 5. Optional: OCR for Scanned Receipts

For `/pdf` receipt extraction from scanned (image-based) PDFs, install:

- **Tesseract OCR**: `brew install tesseract` (macOS) or [download](https://github.com/tesseract-ocr/tesseract)
- **Poppler** (for pdf2image): `brew install poppler` (macOS)

Without these, only digital/text-based PDF receipts will work.

## Usage (Group Chat Only)

Mention the bot with @YourBotName for all commands:

| Command | Description |
|---------|-------------|
| `@BotName /start_meeting` | Start tracking meeting time |
| `@BotName /end_meeting` | End meeting and update Time Sheet |
| `@BotName /summary` | Get summary of times and budget |
| `@BotName /update @username minutes` | Manually add time (e.g. /update @Audrey 60) |
| `@BotName /pdf` | Then reply with PDF receipt for extraction |
| Send PDF/Word + `@BotName` | Upload document to Google Drive |

## Time Sheet Structure

- Each user (Andrew, Anna, Audrey, Jonathan, Nathaniel) has their own sheet in the main spreadsheet
- Sheet gids are configured in `user_sheets` (from URL `#gid=123`)
- Columns: Date | Time Start | Time End | Duration (mins) | Notes
- Duration = Time End - Time Start

## SOA (Statement of Account)

- Separate spreadsheet configured via `soa_spreadsheet_id`
- Columns: Date | Item | Price | Qty | Total | Budget Left
- Updated when `/pdf` receipt extraction succeeds

## PDF & Image Receipt Extraction Flow

**Supports:** PDF, JPG, JPEG, PNG, WEBP

1. **Text Extraction**:
   - **Images**: Gemini Vision first (most reliable), then Tesseract OCR fallback
   - **PDF**: pdfplumber (digital) → Tesseract OCR (scanned) → Gemini Vision fallback
2. **Gemini**: Reads the extracted text, understands context, and structures it into SOA parameters (Date, Item, Price, Qty, Total)
3. **Fallback**: If Gemini is not configured or fails, falls back to regex/table parsing (PDF only)

**Requirements:**
- `gemini_api_key` in api_keys.json (for Gemini Vision and text structuring)
- Tesseract OCR: `brew install tesseract` (fallback when Gemini Vision fails)
- Poppler: `brew install poppler` (for scanned PDFs)

**Tip:** Send receipts as a **document** (not photo) for best results. Telegram compresses photos heavily.

## User Mapping

Edit `config.py` to map Telegram usernames to display names (Andrew, Anna, Audrey, Jonathan, Nathaniel).
