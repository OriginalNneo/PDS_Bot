"""
Telegram Bot for meeting time tracking, document management, and receipt extraction.
Only responds to the configured allowed user (by Telegram ID).
"""
import os
import asyncio
import tempfile
import logging
from datetime import datetime
from pathlib import Path

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import load_config, get_bot_token, USER_MAPPING
from typing import Optional

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# In-memory meeting tracking: {chat_id: {user_id: start_time}}
active_meetings: dict[int, dict[int, datetime]] = {}

# Media group collector: {media_group_id: {"chat_id": int, "context": Context, "items": [(file_id, doc, photo, suffix), ...]}}
_pending_media_groups: dict[str, dict] = {}
_media_group_lock = asyncio.Lock()
_MEDIA_GROUP_DELAY = 2.5  # seconds to wait for all items in album

# Paths
PROJECT_ROOT = Path(__file__).parent
CREDENTIALS_PATH = PROJECT_ROOT / "credentials.json"
CONFIG_PATH = PROJECT_ROOT / "api_keys.json"


def get_user_display_name(update: Update) -> str:
    """Get display name for the user from Telegram or config mapping."""
    user = update.effective_user
    if not user:
        return "Unknown"
    config = load_config()
    # Check allowed_user_names mapping (user_id -> display_name) for multiple users
    allowed_user_names = config.get("allowed_user_names", {})
    if isinstance(allowed_user_names, dict):
        uid_str = str(user.id)
        if uid_str in allowed_user_names:
            return allowed_user_names[uid_str]
    # Fallback: single allowed user with allowed_user_display_name
    allowed_user_id = config.get("allowed_user_id")
    if allowed_user_id and user.id == int(allowed_user_id):
        return config.get("allowed_user_display_name", "Nathaniel")
    # Try username first (USER_MAPPING)
    username = (user.username or "").lower()
    for key, display in USER_MAPPING.items():
        if key in username or username in key:
            return display
    # Try first name
    first_name = (user.first_name or "").lower()
    for key, display in USER_MAPPING.items():
        if key in first_name or first_name in key:
            return display
    return user.first_name or user.username or "Unknown"


async def start_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start tracking meeting time for the user."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user_name = get_user_display_name(update)

    if chat_id not in active_meetings:
        active_meetings[chat_id] = {}

    if user_id in active_meetings[chat_id]:
        await update.message.reply_text(
            f"⏱️ {user_name}, you already have an active meeting. Use /end_meeting to end it first."
        )
        return

    active_meetings[chat_id][user_id] = datetime.now()
    await update.message.reply_text(
        f"⏱️ Meeting started for {user_name} at {active_meetings[chat_id][user_id].strftime('%H:%M')}."
    )


async def end_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """End meeting and update Google Sheets with duration."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user_name = get_user_display_name(update)

    if chat_id not in active_meetings or user_id not in active_meetings[chat_id]:
        await update.message.reply_text(
            f"⏱️ {user_name}, you don't have an active meeting. Use /start_meeting first."
        )
        return

    time_start = active_meetings[chat_id].pop(user_id)
    time_end = datetime.now()
    if not active_meetings[chat_id]:
        del active_meetings[chat_id]

    try:
        config = load_config()
        creds_path = str(_resolve_creds_path(config.get("google_credentials") or str(CREDENTIALS_PATH)))
        spreadsheet_id = config.get("spreadsheet_id")
        if not spreadsheet_id or not Path(creds_path).exists():
            await update.message.reply_text(
                "❌ Google Sheets not configured. Please set google_credentials and spreadsheet_id in api_keys.json"
            )
            return

        from services.sheets_service import SheetsService
        user_sheets = config.get("user_sheets", {})
        soa_spreadsheet_id = config.get("soa_spreadsheet_id")
        sheets = SheetsService(
            creds_path, spreadsheet_id,
            user_sheets=user_sheets,
            soa_spreadsheet_id=soa_spreadsheet_id,
        )
        duration_mins = sheets.record_meeting_for_all(time_start, time_end)

        await update.message.reply_text(
            f"✅ Meeting ended. Duration: {duration_mins:.1f} minutes. "
            f"Time sheets updated for all members."
        )
    except PermissionError as e:
        logger.exception("Permission denied accessing Google Sheets")
        msg = str(e.__cause__) if e.__cause__ else ""
        if "API has not been used" in msg or "is disabled" in msg:
            await update.message.reply_text(
                "❌ Google Sheets API is not enabled. Enable it at:\n"
                "console.cloud.google.com/apis/library/sheets.googleapis.com"
            )
        else:
            try:
                import json
                cfg = load_config()
                creds_path = _resolve_creds_path(cfg.get("google_credentials") or str(CREDENTIALS_PATH))
                creds = json.load(open(creds_path))
                email = creds.get("client_email", "See credentials.json")
            except Exception:
                email = "See credentials.json → client_email"
            await update.message.reply_text(
                f"❌ Permission denied. Share your Google Sheet with this email as Editor:\n\n{email}"
            )
    except Exception as e:
        logger.exception("Error updating time sheet")
        err_msg = str(e).strip() or repr(e) or "Unknown error"
        await update.message.reply_text(f"❌ Error updating time sheet: {err_msg}")


def _resolve_creds_path(creds_path: str) -> Path:
    """Resolve credentials path to absolute path."""
    p = Path(creds_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / creds_path
    return p


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display summary of times and budget."""
    try:
        config = load_config()
        creds_path = config.get("google_credentials") or str(CREDENTIALS_PATH)
        creds_path = str(_resolve_creds_path(creds_path))
        spreadsheet_id = config.get("spreadsheet_id")
        if not spreadsheet_id or not Path(creds_path).exists():
            await update.message.reply_text(
                "❌ Google Sheets not configured. Please set google_credentials and spreadsheet_id in api_keys.json"
            )
            return

        from services.sheets_service import SheetsService
        user_sheets = config.get("user_sheets", {})
        soa_spreadsheet_id = config.get("soa_spreadsheet_id")
        sheets = SheetsService(
            creds_path, spreadsheet_id,
            user_sheets=user_sheets,
            soa_spreadsheet_id=soa_spreadsheet_id,
        )
        data = sheets.get_summary()

        date_str = datetime.now().strftime("%d/%m/%Y")
        msg = f"📊 Summary ({date_str})\n\n"
        msg += f"Andrew_Time: {data.get('Andrew', 0):.1f} mins\n"
        msg += f"Anna_Time: {data.get('Anna', 0):.1f} mins\n"
        msg += f"Audrey_Time: {data.get('Audrey', 0):.1f} mins\n"
        msg += f"Jonathan_Time: {data.get('Jonathan', 0):.1f} mins\n"
        msg += f"Nathaniel_Time: {data.get('Nathaniel', 0):.1f} mins\n\n"
        msg += f"Budget Spent: {data.get('budget_spent', 0):.2f}\n"
        msg += f"Budget Left: {data.get('budget_left', 0):.2f}\n"

        await update.message.reply_text(msg)
    except PermissionError as e:
        logger.exception("Permission denied accessing Google Sheets")
        msg = str(e.__cause__) if e.__cause__ else ""
        if "API has not been used" in msg or "is disabled" in msg:
            await update.message.reply_text(
                "❌ Google Sheets API is not enabled. Enable it at:\n"
                "console.cloud.google.com/apis/library/sheets.googleapis.com"
            )
        else:
            try:
                import json
                cfg = load_config()
                creds_path = _resolve_creds_path(cfg.get("google_credentials") or str(CREDENTIALS_PATH))
                creds = json.load(open(creds_path))
                email = creds.get("client_email", "See credentials.json")
            except Exception:
                email = "See credentials.json → client_email"
            await update.message.reply_text(
                f"❌ Permission denied. Share your Google Sheet with this email as Editor:\n\n{email}"
            )
    except Exception as e:
        logger.exception("Error getting summary")
        err_msg = str(e).strip() or repr(e) or "Unknown error"
        await update.message.reply_text(f"❌ Error: {err_msg}")


async def handle_document_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upload PDF or Word documents to Google Drive in DD/MM/YYYY folder."""
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    file_name = doc.file_name or "document"
    ext = Path(file_name).suffix.lower()

    if ext not in (".pdf", ".doc", ".docx"):
        await update.message.reply_text(
            "📄 I only accept PDF and Word (.doc, .docx) documents for upload."
        )
        return

    try:
        config = load_config()
        creds_path = str(_resolve_creds_path(config.get("google_credentials") or str(CREDENTIALS_PATH)))
        drive_folder_id = config.get("drive_folder_id")
        if not drive_folder_id or not Path(creds_path).exists():
            await update.message.reply_text(
                "❌ Google Drive not configured. Please set google_credentials and drive_folder_id in api_keys.json"
            )
            return

        file = await context.bot.get_file(doc.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            await file.download_to_drive(custom_path=tmp.name)
            tmp_path = tmp.name

        try:
            from services.drive_service import DriveService
            drive = DriveService(creds_path, drive_folder_id)
            drive.upload_file(tmp_path)
            folder_name = datetime.now().strftime("%d/%m/%Y")
            await update.message.reply_text(
                f"✅ File uploaded to Google Drive in folder {folder_name}."
            )
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.exception("Error uploading document")
        await update.message.reply_text(f"❌ Error uploading: {str(e)}")




def _infer_suffix_from_mime(mime_type: str) -> str:
    """Infer file suffix from MIME type for receipt extraction."""
    if not mime_type:
        return ""
    m = (mime_type or "").lower()
    if "image/jpeg" in m or "image/jpg" in m:
        return ".jpg"
    if "image/png" in m:
        return ".png"
    if "image/webp" in m:
        return ".webp"
    if "application/pdf" in m:
        return ".pdf"
    return ""


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route documents: receipt extraction (PDF/image reply to /pdf) or Drive upload."""
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    file_name = doc.file_name or ""
    ext = Path(file_name).suffix.lower()
    if not ext and getattr(doc, "mime_type", None):
        ext = _infer_suffix_from_mime(doc.mime_type)

    # PDF or image with /pdf -> receipt extraction (or part of receipt media group)
    mg_id = getattr(update.message, "media_group_id", None)
    is_receipt = ext in (".pdf", ".jpg", ".jpeg", ".png", ".webp") and (
        is_for_receipt_extraction(update) or (mg_id and mg_id in _pending_media_groups)
    )
    if is_receipt:
        if mg_id:
            async with _media_group_lock:
                if mg_id not in _pending_media_groups:
                    _pending_media_groups[mg_id] = {
                        "chat_id": update.effective_chat.id,
                        "context": context,
                        "items": [],
                    }
                    asyncio.create_task(_flush_media_group(context, mg_id))
                _pending_media_groups[mg_id]["items"].append((doc.file_id, doc, None, ext or ".jpg"))
            return
        await handle_receipt_extraction(update, context, doc=doc, suffix=ext or ".jpg")
        return

    # PDF or Word -> upload to Drive
    if ext in (".pdf", ".doc", ".docx"):
        await handle_document_upload(update, context)
        return

    await update.message.reply_text(
        "📄 I only accept PDF, images (JPG/PNG/WEBP), and Word (.doc, .docx) documents."
    )


async def _process_single_receipt(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    file_id: str,
    suffix: str,
    doc=None,
    photo=None,
) -> tuple[list[dict], bool, Optional[str], str, Optional[str]]:
    """Process one receipt file. Returns (items, success, error_msg, extraction_method, tmp_path).
    Caller must upload tmp_path to Drive and delete it when done."""
    file = await context.bot.get_file(doc.file_id if doc else photo.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        await file.download_to_drive(custom_path=tmp.name)
        tmp_path = tmp.name
    try:
        from services.pdf_service import PDFExtractor
        extractor = PDFExtractor()
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: extractor.extract_receipt_data(tmp_path)),
            timeout=120,
        )
        return (*result, tmp_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def _flush_media_group(context: ContextTypes.DEFAULT_TYPE, mg_id: str) -> None:
    """After delay, process all collected items in a media group."""
    await asyncio.sleep(_MEDIA_GROUP_DELAY)
    async with _media_group_lock:
        data = _pending_media_groups.pop(mg_id, None)
    if not data or not data.get("items"):
        return
    n = len(data["items"])
    await context.bot.send_message(data["chat_id"], f"📄 Processing {n} receipt(s)...")
    await _process_media_group_receipts(context, data["chat_id"], data["items"])


async def _process_media_group_receipts(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    items: list[tuple],
) -> None:
    """Process all receipts in a media group and send one combined reply."""
    try:
        config = load_config()
        creds_path = str(_resolve_creds_path(config.get("google_credentials") or str(CREDENTIALS_PATH)))
        spreadsheet_id = config.get("spreadsheet_id")
        if not spreadsheet_id or not Path(creds_path).exists():
            await context.bot.send_message(chat_id, "❌ Google Sheets not configured.")
            return

        from services.sheets_service import SheetsService
        user_sheets = config.get("user_sheets", {})
        soa_spreadsheet_id = config.get("soa_spreadsheet_id")
        sheets = SheetsService(creds_path, spreadsheet_id, user_sheets=user_sheets, soa_spreadsheet_id=soa_spreadsheet_id)

        all_items: list[dict] = []
        all_summary_lines: list[str] = []
        total_amount = 0.0
        methods_used: set[str] = set()
        errors: list[str] = []
        drive_folder_id = config.get("drive_folder_id")
        drive_uploaded = False
        drive_error: Optional[str] = None

        for i, (file_id, doc, photo, suffix) in enumerate(items):
            tmp_path = None
            try:
                items_data, success, error_msg, extraction_method, tmp_path = await _process_single_receipt(
                    context, chat_id, file_id, suffix, doc=doc, photo=photo
                )
                if success and items_data:
                    sheets.update_soa(items_data)
                    all_items.extend(items_data)
                    methods_used.add(extraction_method)
                    for it in items_data:
                        date_bought = str(it.get("Date", "")).strip()
                        name = str(it.get("Item", "Unknown")).strip()
                        total = it.get("Total", 0)
                        try:
                            amt = float(total)
                            total_amount += amt
                            all_summary_lines.append(f"{date_bought} | {name} | ${amt:,.2f}")
                        except (TypeError, ValueError):
                            all_summary_lines.append(f"{date_bought} | {name} | -")
                    # Upload receipt to Drive (blocking - run in executor)
                    if drive_folder_id and tmp_path and Path(tmp_path).exists():
                        try:
                            from services.drive_service import DriveService
                            drive = DriveService(creds_path, drive_folder_id)
                            loop = asyncio.get_event_loop()
                            await loop.run_in_executor(None, lambda p=tmp_path: drive.upload_file(p))
                            drive_uploaded = True
                        except Exception as e:
                            drive_error = drive_error or str(e)
                            logger.warning("Drive upload failed for receipt %d: %s", i + 1, e)
                else:
                    errors.append(f"Receipt {i + 1}: {error_msg or 'No data extracted'}")
            except asyncio.TimeoutError:
                errors.append(f"Receipt {i + 1}: Timed out")
            except Exception as e:
                logger.exception("Error processing receipt %d in media group", i + 1)
                errors.append(f"Receipt {i + 1}: {str(e)}")
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        if all_summary_lines:
            folder_name = datetime.now().strftime("%d/%m/%Y")
            summary_str = "\n".join(all_summary_lines)
            method_label = ", ".join({
                "gemini_vision": "Gemini Vision",
                "gemini_text": "Gemini",
                "pdfplumber_tables": "pdfplumber",
                "regex_fallback": "Fallback",
            }.get(m, m) for m in methods_used)
            msg = f"✅ Processed {len(items)} receipt(s). Data updated:\n{summary_str}\n\nTotal amount purchased: ${total_amount:,.2f}"
            if drive_uploaded:
                msg += f"\n\n📁 Receipts uploaded to Google Drive in folder {folder_name}."
            elif drive_error:
                msg += f"\n\n⚠️ Drive upload failed: {drive_error}"
            msg += f"\n\n_(via {method_label})_"
            if errors:
                msg += f"\n\n⚠️ Some failed: {'; '.join(errors)}"
            await context.bot.send_message(chat_id, msg)
        elif errors:
            await context.bot.send_message(chat_id, f"❌ All receipts failed:\n" + "\n".join(errors))
        else:
            await context.bot.send_message(chat_id, "❌ No data could be extracted from the receipts.")
    except Exception as e:
        logger.exception("Error processing media group receipts")
        await context.bot.send_message(chat_id, f"❌ Error: {str(e)}")


async def handle_receipt_extraction(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    doc=None,
    photo=None,
    suffix: str = ".pdf",
) -> None:
    """Extract receipt data from PDF or image and update SOA sheet."""
    try:
        config = load_config()
        creds_path = str(_resolve_creds_path(config.get("google_credentials") or str(CREDENTIALS_PATH)))
        spreadsheet_id = config.get("spreadsheet_id")
        if not spreadsheet_id or not Path(creds_path).exists():
            await update.message.reply_text(
                "❌ Google Sheets not configured. Please set credentials in api_keys.json"
            )
            return

        # Get file to download - either document or photo
        if doc:
            file = await context.bot.get_file(doc.file_id)
        elif photo:
            file = await context.bot.get_file(photo.file_id)
        else:
            await update.message.reply_text(
                "📄 Please send a PDF or image (JPG/PNG) receipt as attachment, or reply to /pdf with the file."
            )
            return

        await update.message.reply_text("📄 Received PDF(s). Will process it now.")

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            await file.download_to_drive(custom_path=tmp.name)
            tmp_path = tmp.name

        try:
            from services.pdf_service import PDFExtractor
            from services.sheets_service import SheetsService

            # Run extraction in executor - pdfplumber/Gemini are blocking and can freeze the bot
            loop = asyncio.get_event_loop()
            extractor = PDFExtractor()
            try:
                items, success, error_msg, extraction_method = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: extractor.extract_receipt_data(tmp_path),
                    ),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                await update.message.reply_text(
                    "❌ Processing timed out (2 min). The file may be too large or complex."
                )
                return

            if not success:
                await update.message.reply_text(f"❌ {error_msg}")
                return

            user_sheets = config.get("user_sheets", {})
            soa_spreadsheet_id = config.get("soa_spreadsheet_id")
            sheets = SheetsService(
                creds_path, spreadsheet_id,
                user_sheets=user_sheets,
                soa_spreadsheet_id=soa_spreadsheet_id,
            )
            sheets.update_soa(items)
            # Upload receipt to Drive (blocking - run in executor)
            drive_uploaded = False
            drive_error = None
            drive_folder_id = config.get("drive_folder_id")
            if drive_folder_id and Path(tmp_path).exists():
                try:
                    from services.drive_service import DriveService
                    drive = DriveService(creds_path, drive_folder_id)
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, lambda: drive.upload_file(tmp_path))
                    drive_uploaded = True
                except Exception as e:
                    drive_error = str(e)
                    logger.warning("Drive upload failed: %s", e)
            elif not drive_folder_id:
                drive_error = "drive_folder_id not set in api_keys.json"
            # Build summary: Date bought | item | Amount (each line), then Total amount purchased
            summary_lines = []
            total_amount = 0.0
            for it in items:
                date_bought = str(it.get("Date", "")).strip()
                name = str(it.get("Item", "Unknown")).strip()
                total = it.get("Total", 0)
                try:
                    amt = float(total)
                    total_amount += amt
                    summary_lines.append(f"{date_bought} | {name} | ${amt:,.2f}")
                except (TypeError, ValueError):
                    summary_lines.append(f"{date_bought} | {name} | -")
            if summary_lines:
                folder_name = datetime.now().strftime("%d/%m/%Y")
                summary_str = "\n".join(summary_lines)
                method_label = {
                    "gemini_vision": "Gemini Vision (image)",
                    "gemini_text": "Gemini (text)",
                    "pdfplumber_tables": "pdfplumber",
                    "regex_fallback": "Fallback (regex)",
                }.get(extraction_method, extraction_method)
                msg = f"✅ Data Extracted and Updated the Database:\n{summary_str}\n\nTotal amount purchased: ${total_amount:,.2f}"
                if drive_uploaded:
                    msg += f"\n\n📁 Receipt uploaded to Google Drive in folder {folder_name}."
                elif drive_error:
                    msg += f"\n\n⚠️ Drive upload failed: {drive_error}"
                    if "403" in drive_error or "permission" in drive_error.lower() or "has not been used" in drive_error.lower():
                        try:
                            import json
                            creds_path_resolved = str(_resolve_creds_path(config.get("google_credentials") or str(CREDENTIALS_PATH)))
                            creds = json.load(open(creds_path_resolved))
                            email = creds.get("client_email", "")
                            if email:
                                msg += f"\n\nShare the Drive folder (drive_folder_id) with {email} as Editor. Enable Drive API at console.cloud.google.com."
                        except Exception:
                            pass
                msg += f"\n\n_(via {method_label})_"
                await update.message.reply_text(msg)
            else:
                await update.message.reply_text("✅ Data Extracted and Updated the Database")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    except Exception as e:
        logger.exception("Error processing receipt")
        err_msg = str(e).strip() or repr(e) or "Unknown error"
        await update.message.reply_text(f"❌ Error: {err_msg}")


def is_for_receipt_extraction(update: Update) -> bool:
    """Check if message is for receipt extraction.
    
    Returns True if:
    - Message has /pdf in caption (e.g. image sent with /pdf caption)
    - Message is a reply to /pdf prompt
    """
    msg = update.message
    if not msg:
        return False
    # Check if /pdf is in caption of current message
    caption = (msg.caption or "").lower()
    if "/pdf" in caption:
        return True
    # Check if replying to /pdf message
    if msg.reply_to_message:
        reply_text = (msg.reply_to_message.text or msg.reply_to_message.caption or "").lower()
        if "/pdf" in reply_text:
            return True
    return False


async def handle_photo_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Extract receipt from photo with /pdf caption or replying to /pdf."""
    if not update.message or not update.message.photo:
        return
    if not is_for_receipt_extraction(update):
        return

    photo = update.message.photo[-1]  # largest size
    mg_id = getattr(update.message, "media_group_id", None)
    if mg_id:
        async with _media_group_lock:
            if mg_id not in _pending_media_groups:
                if not is_for_receipt_extraction(update):
                    return  # First photo in group needs /pdf caption
                _pending_media_groups[mg_id] = {
                    "chat_id": update.effective_chat.id,
                    "context": context,
                    "items": [],
                }
                asyncio.create_task(_flush_media_group(context, mg_id))
            _pending_media_groups[mg_id]["items"].append((photo.file_id, None, photo, ".jpg"))
        return
    await handle_receipt_extraction(update, context, photo=photo, suffix=".jpg")


async def pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt user to send PDF or image for receipt extraction."""
    await update.message.reply_text(
        "📄 Send PDF or image receipt(s) with /pdf in the caption.\n\n"
        "• Multiple receipts in one image? I'll extract all of them.\n"
        "• Multiple files? Select them together and send as an album (same caption)."
    )


def _parse_user_name(s: str) -> Optional[str]:
    """Parse @Audrey or Audrey to display name (Andrew, Anna, Audrey, Jonathan, Nathaniel)."""
    if not s:
        return None
    s = str(s).strip().lstrip("@").lower()
    for key, display in USER_MAPPING.items():
        if key in s or s in key:
            return display
    # Direct match
    if s == "andrew":
        return "Andrew"
    if s == "anna":
        return "Anna"
    if s == "audrey":
        return "Audrey"
    if s == "jonathan":
        return "Jonathan"
    if s == "nathaniel":
        return "Nathaniel"
    return None


async def update_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually add time for a user. Usage: /update @Audrey 60"""
    if not update.message or not update.message.text:
        await update.message.reply_text(
            "Usage: /update @username minutes\nExample: /update @Audrey 60"
        )
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /update @username minutes\nExample: /update @Audrey 60"
        )
        return

    user_arg = args[0]
    try:
        minutes = float(args[1])
        if minutes <= 0:
            raise ValueError("Minutes must be positive")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Invalid minutes. Use a positive number (e.g. 60).")
        return

    user_name = _parse_user_name(user_arg)
    if not user_name:
        await update.message.reply_text(
            f"❌ Unknown user: {user_arg}. Use @Audrey, @Andrew, @Anna, @Jonathan, or @Nathaniel."
        )
        return

    try:
        config = load_config()
        creds_path = str(_resolve_creds_path(config.get("google_credentials") or str(CREDENTIALS_PATH)))
        spreadsheet_id = config.get("spreadsheet_id")
        if not spreadsheet_id or not Path(creds_path).exists():
            await update.message.reply_text(
                "❌ Google Sheets not configured. Please set credentials in api_keys.json"
            )
            return

        from services.sheets_service import SheetsService
        user_sheets = config.get("user_sheets", {})
        soa_spreadsheet_id = config.get("soa_spreadsheet_id")
        sheets = SheetsService(
            creds_path, spreadsheet_id,
            user_sheets=user_sheets,
            soa_spreadsheet_id=soa_spreadsheet_id,
        )
        sheets.add_manual_time(user_name, minutes)

        date_str = datetime.now().strftime("%d/%m/%Y")
        await update.message.reply_text(
            f"✅ Added {minutes:.1f} minutes for {user_name} on {date_str}."
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
    except Exception as e:
        logger.exception("Error updating time")
        err_msg = str(e).strip() or repr(e) or "Unknown error"
        await update.message.reply_text(f"❌ Error: {err_msg}")


async def post_init(application: Application) -> None:
    """Set bot commands menu (shown when user types /)."""
    commands = [
        BotCommand("start_meeting", "Start meeting time tracking"),
        BotCommand("end_meeting", "End meeting and update time sheet"),
        BotCommand("summary", "Show time and budget summary"),
        BotCommand("pdf", "Send receipt(s) for extraction"),
        BotCommand("update", "Add manual time for a user (e.g. /update @Audrey 60)"),
    ]
    await application.bot.set_my_commands(commands)


def main() -> None:
    """Run the bot."""
    token = get_bot_token()
    config = load_config()
    # Support both allowed_user_id (single) and allowed_user_ids (list)
    raw_ids = config.get("allowed_user_ids") or config.get("allowed_user_id")
    if raw_ids is None:
        allowed_user_ids = []
    elif isinstance(raw_ids, list):
        allowed_user_ids = [int(uid) for uid in raw_ids]
    else:
        allowed_user_ids = [int(raw_ids)]
    if not allowed_user_ids:
        raise ValueError(
            "Please set allowed_user_id or allowed_user_ids in api_keys.json "
            "(your Telegram user ID(s)). Get your ID from @userinfobot on Telegram."
        )
    user_filter = filters.User(user_id=allowed_user_ids)

    application = Application.builder().token(token).post_init(post_init).build()

    # Command handlers - only for allowed user
    application.add_handler(
        CommandHandler("start_meeting", start_meeting, filters=user_filter)
    )
    application.add_handler(
        CommandHandler("end_meeting", end_meeting, filters=user_filter)
    )
    application.add_handler(
        CommandHandler("summary", summary, filters=user_filter)
    )
    application.add_handler(
        CommandHandler("pdf", pdf_command, filters=user_filter)
    )
    application.add_handler(
        CommandHandler("update", update_time, filters=user_filter)
    )

    # Document handler - PDF, images, Word - routes to receipt extraction or Drive upload
    application.add_handler(
        MessageHandler(
            user_filter & filters.Document.ALL,
            handle_document,
        )
    )

    # Photo handler - for receipt extraction when replying to /pdf
    application.add_handler(
        MessageHandler(
            user_filter & filters.PHOTO,
            handle_photo_receipt,
        )
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
