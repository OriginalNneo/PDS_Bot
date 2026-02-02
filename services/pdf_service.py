"""PDF and image extraction service for receipt processing with OCR.

Flow: 1) PDF Extractor (pdfplumber + OCR) or Image (pytesseract) extracts raw text
      2) Gemini reads text and structures it into SOA parameters (Date, Item, Price, Qty, Total)

Supports: PDF, JPG, JPEG, PNG
"""
import re
import json
import logging
import pdfplumber

logger = logging.getLogger(__name__)
from pathlib import Path
from datetime import datetime
from typing import Optional

# Optional OCR for scanned receipts and images
try:
    import pytesseract
    from pdf2image import convert_from_path
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# Supported file extensions for receipt extraction
RECEIPT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}

# SOA columns for receipt data
SOA_COLUMNS = ["Date", "Item", "Price", "Qty", "Total"]


class PDFExtractor:
    """Extract receipt data from PDFs using pdfplumber and optional OCR."""

    # Regex patterns for amount extraction
    CURRENCY_PATTERN = re.compile(
        r"(?:SGD|USD|\$|€|£)\s*([\d,]+\.?\d*)"
    )
    DECIMAL_PATTERN = re.compile(r"[\d,]+\.\d{2}")
    TOTAL_PATTERNS = [
        re.compile(r"total[:\s]*\$?([\d,]+\.?\d*)", re.I),
        re.compile(r"grand\s*total[:\s]*\$?([\d,]+\.?\d*)", re.I),
        re.compile(r"amount\s*due[:\s]*\$?([\d,]+\.?\d*)", re.I),
        re.compile(r"subtotal[:\s]*\$?([\d,]+\.?\d*)", re.I),
    ]

    def __init__(self):
        """Initialize the PDF extractor."""
        self.ocr_available = OCR_AVAILABLE

    def _extract_text_pdfplumber(self, pdf_path: str) -> str:
        """Extract text using pdfplumber (for digital PDFs)."""
        text_parts = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
                # Also try table extraction for structured receipts
                tables = page.extract_tables()
                for table in tables:
                    if table:
                        for row in table:
                            text_parts.append(" ".join(str(cell or "") for cell in row))
        return "\n".join(text_parts)

    def _extract_text_ocr(self, pdf_path: str) -> str:
        """Extract text using OCR (for scanned PDFs)."""
        if not self.ocr_available:
            logger.warning("OCR not available (Tesseract/Poppler may be missing)")
            return ""
        try:
            images = convert_from_path(pdf_path)
            text_parts = []
            for img in images:
                text = pytesseract.image_to_string(img)
                text_parts.append(text)
            return "\n".join(text_parts)
        except Exception as e:
            logger.warning("OCR failed for PDF: %s", e)
            return ""

    def _extract_text_from_image(self, image_path: str) -> str:
        """Extract text from image (JPG, PNG) using pytesseract OCR."""
        if not self.ocr_available:
            logger.warning("OCR not available (Tesseract may be missing)")
            return ""
        try:
            img = Image.open(image_path)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            return pytesseract.image_to_string(img)
        except Exception as e:
            logger.warning("OCR failed for image: %s", e)
            return ""

    def _extract_text_via_gemini_vision(self, image_path: str) -> str:
        """Extract text from image using Gemini Vision API (fallback when OCR fails)."""
        try:
            from services.gemini_service import generate_content_with_image
        except ImportError:
            return ""
        prompt = (
            "Extract EVERY line of text from this receipt/image. Do NOT skip, summarize, or omit anything. "
            "Include every single item name, every quantity, every price, every subtotal, tax, and total. "
            "Return only the raw text exactly as it appears, preserving layout and line breaks."
        )
        result = generate_content_with_image(prompt, image_path)
        return result or ""

    def _extract_text_from_pdf_via_gemini_vision(self, pdf_path: str) -> str:
        """Extract text from first page of PDF using Gemini Vision (fallback when pdfplumber/OCR fail)."""
        if not self.ocr_available:
            return ""
        try:
            images = convert_from_path(pdf_path, first_page=1, last_page=1)
            if not images:
                return ""
            # Save first page to temp file for Gemini
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                images[0].save(tmp.name, "PNG")
                tmp_path = tmp.name
            try:
                return self._extract_text_via_gemini_vision(tmp_path)
            finally:
                import os
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception as e:
            logger.warning("Gemini Vision fallback for PDF failed: %s", e)
            return ""

    def extract_text(self, file_path: str) -> tuple[str, bool, Optional[str]]:
        """Extract raw text from PDF or image (JPG, PNG).

        PDF: pdfplumber first, then OCR fallback for scanned docs.
        Image: pytesseract OCR directly.

        Args:
            file_path: Path to the PDF or image file

        Returns:
            Tuple of (extracted text, success, error_message)
        """
        path = Path(file_path)
        if not path.exists():
            return "", False, "File not found"

        ext = path.suffix.lower()
        text = ""

        if ext == ".pdf":
            # Step 1: Try pdfplumber (digital PDFs)
            text = self._extract_text_pdfplumber(file_path)
            # Step 2: If little/no text, try OCR (scanned receipts)
            if not text or len(text.strip()) < 20:
                if self.ocr_available:
                    text = self._extract_text_ocr(file_path)
            # Step 3: If still no text, try Gemini Vision on first page (requires pdf2image)
            if (not text or len(text.strip()) < 20) and self.ocr_available:
                vision_text = self._extract_text_from_pdf_via_gemini_vision(file_path)
                if vision_text and len(vision_text.strip()) >= 20:
                    text = vision_text
                    logger.info("Used Gemini Vision fallback for PDF (pdfplumber/OCR returned little text)")
        elif ext in (".jpg", ".jpeg", ".png", ".webp"):
            # Images: try Gemini Vision FIRST (most reliable), then OCR fallback
            vision_text = self._extract_text_via_gemini_vision(file_path)
            if vision_text and len(vision_text.strip()) >= 20:
                text = vision_text
                logger.info("Used Gemini Vision for image extraction")
            elif self.ocr_available:
                text = self._extract_text_from_image(file_path)
        else:
            return "", False, f"Unsupported file type: {ext}. Use PDF, JPG, PNG, or WEBP."

        if not text or len(text.strip()) < 20:
            logger.warning(
                "Text extraction failed: path=%s ext=%s ocr_available=%s extracted_len=%d",
                file_path, ext, self.ocr_available, len(text.strip()) if text else 0,
            )
            return "", False, (
                "Unable to extract text. The document may be scanned with poor quality - "
                "please ensure the receipt is clear and legible."
            )

        return text.strip(), True, None

    def _parse_amount(self, value: str) -> Optional[float]:
        """Parse a string amount to float."""
        if not value:
            return None
        cleaned = re.sub(r"[^\d.]", "", value.replace(",", ""))
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _extract_totals_from_text(self, text: str) -> list[float]:
        """Extract total amounts from receipt text."""
        amounts = []
        for pattern in self.TOTAL_PATTERNS:
            for match in pattern.finditer(text):
                amt = self._parse_amount(match.group(1))
                if amt and amt > 0:
                    amounts.append(amt)
        # Fallback: find all decimal amounts
        if not amounts:
            for match in self.DECIMAL_PATTERN.finditer(text):
                amt = self._parse_amount(match.group(0))
                if amt and amt > 0 and amt < 100000:  # Sanity check
                    amounts.append(amt)
        return amounts

    def _extract_items_from_tables(self, pdf_path: str) -> list[dict]:
        """Extract item rows from PDF tables."""
        items = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    # Assume first row is header
                    headers = [str(h or "").strip().lower() for h in table[0]]
                    for row in table[1:]:
                        row_dict = dict(zip(headers, [str(c or "").strip() for c in row]))
                        # Try to identify item, price, qty, total
                        item = row_dict.get("item", row_dict.get("description", row_dict.get("name", "")))
                        price = self._parse_amount(row_dict.get("price", row_dict.get("unit price", "")))
                        qty = self._parse_amount(row_dict.get("qty", row_dict.get("quantity", "1"))) or 1
                        total = self._parse_amount(row_dict.get("total", row_dict.get("amount", "")))
                        if item or total:
                            items.append({
                                "Item": item or "Unknown",
                                "Price": price or 0,
                                "Qty": qty,
                                "Total": total or (price * qty if price else 0),
                            })
        return items

    def _parse_receipt_directly_from_pdf_image(self, pdf_path: str) -> Optional[list[dict]]:
        """Convert PDF pages to images and use Gemini Vision. Handles multi-page PDFs."""
        if not self.ocr_available:
            return None
        try:
            images = convert_from_path(pdf_path)  # All pages
            if not images:
                return None
            import tempfile
            import os
            all_items: list[dict] = []
            for i, img in enumerate(images):
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    img.save(tmp.name, "PNG")
                    tmp_path = tmp.name
                try:
                    page_items = self._parse_receipt_directly_from_image(tmp_path)
                    if page_items:
                        all_items.extend(page_items)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            return all_items if all_items else None
        except Exception as e:
            logger.warning("Direct PDF Vision parsing failed: %s", e)
        return None

    def _parse_receipt_directly_from_image(self, image_path: str) -> Optional[list[dict]]:
        """Use Gemini Vision to extract receipt items directly from image (single-step, most complete)."""
        try:
            from services.gemini_service import generate_content_with_image
        except ImportError:
            return None

        prompt = """You are a receipt parser. Look at this image and extract EVERY line item from EVERY receipt.

CRITICAL: If there are MULTIPLE receipts in this image (e.g. two receipts side-by-side, or multiple pages), extract ALL items from ALL receipts. Do NOT skip any receipt.

FORMAT for each item: {"Date": "DD/MM/YYYY", "Item": "exact name", "Price": number, "Qty": number, "Total": number}
- Date: from each receipt (e.g. 09/02/2021, 11/04/2025). Use DD/MM/YYYY.
- Item: EXACT name (e.g. "Bud Light Can", "Custom product/service A"). NEVER "Receipt Total" or "Unknown".
- Price: unit price per single item
- Qty: quantity (e.g. 2 for "2 Bud Light Can")
- Total: line total (Price × Qty). For tax, use the tax amount as Total.

ALWAYS INCLUDE:
- Every item from EVERY receipt visible (left receipt, right receipt, all pages)
- Sales Tax / GST / "Sales Tax (Included)" / "Sales Tax (5%)" as a SEPARATE line for each receipt
- Items with no price: use Total: 0
- Refunds: use negative Total (e.g. "2 Modelo Can ($200)" → Total: -200)

If you see 2 receipts: extract all items from receipt 1, then all items from receipt 2. Return one flat JSON array with every line item from every receipt.

Return ONLY the JSON array. No markdown, no explanation."""

        logger.info("Sending image to Gemini Vision for direct parsing: %s", image_path)
        response = generate_content_with_image(prompt, image_path)
        if not response:
            logger.warning("Gemini Vision returned no response for direct parsing")
            return None
        return self._parse_gemini_json_response(response)

    def _parse_gemini_json_response(self, response: str) -> Optional[list[dict]]:
        """Parse Gemini JSON response into normalized items. Handles markdown and extra text."""
        logger.info("Gemini response (first 500 chars): %s", response[:500] if response else "None")
        if not response or not response.strip():
            return None
        text = response.strip()
        # Remove markdown code blocks
        if "```" in text:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
            if match:
                text = match.group(1).strip()
            else:
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text).strip()
        # Try to find JSON array in response (handles "Here is the data: [...]")
        if not text.startswith("["):
            match = re.search(r"\[[\s\S]*\]", text)
            if match:
                text = match.group(0)

        try:
            items = json.loads(text)
            if not isinstance(items, list):
                logger.warning("Gemini response is not a list")
                return None
            normalized = []
            date_str = datetime.now().strftime("%d/%m/%Y")
            for item in items:
                if not isinstance(item, dict):
                    continue
                row = {
                    "Date": str(item.get("Date", date_str)),
                    "Item": str(item.get("Item", "Unknown")),
                    "Price": item.get("Price", 0),
                    "Qty": item.get("Qty", 1),
                    "Total": item.get("Total", 0),
                }
                try:
                    row["Price"] = float(row["Price"]) if row["Price"] else 0
                    row["Qty"] = float(row["Qty"]) if row["Qty"] else 1
                    row["Total"] = float(row["Total"]) if row["Total"] else 0
                except (ValueError, TypeError):
                    pass
                normalized.append(row)
            logger.info("Parsed %d items from Gemini response", len(normalized))
            return normalized if normalized else None
        except json.JSONDecodeError as e:
            logger.warning("JSON parse error: %s", e)
            return None

    def _parse_receipt_with_gemini(self, text: str) -> Optional[list[dict]]:
        """Use Gemini to read receipt text and structure it into SOA parameters."""
        try:
            from services.gemini_service import generate_content
        except ImportError:
            return None

        prompt = f"""You are a receipt parser. Extract EVERY line item from this receipt text.

CRITICAL: If there are MULTIPLE receipts in this text (different companies, different dates, different sections), extract ALL items from ALL receipts. Return one flat JSON array with every line item from every receipt.

FORMAT: {{"Date": "DD/MM/YYYY", "Item": "exact name", "Price": number, "Qty": number, "Total": number}}
- Date: from each receipt (DD/MM/YYYY)
- Item: EXACT name. NEVER "Receipt Total" or "Unknown".
- Price: unit price per single item
- Qty: quantity
- Total: line total. For Sales Tax use the tax amount as Total, Qty: 1.
- Items with no price: Total: 0
- Refunds: use negative Total

Include: every line from every receipt, AND Sales Tax / GST as a SEPARATE line for each receipt.

Receipt text:
---
{text}
---

Return ONLY the JSON array."""

        response = generate_content(prompt)
        if not response:
            return None
        return self._parse_gemini_json_response(response)

    def extract_receipt_data(self, file_path: str) -> tuple[list[dict], bool, Optional[str], str]:
        """Extract receipt data from PDF or image (JPG, PNG).

        Flow: Images: Try direct Gemini Vision → JSON first (most complete), then text + parse
              PDFs: Extract text, then Gemini parses, fallback to table extraction

        Args:
            file_path: Path to the PDF or image file

        Returns:
            Tuple of (items, success, error_message, extraction_method)
            extraction_method: "gemini_vision" | "gemini_text" | "pdfplumber_tables" | "regex_fallback"
        """
        path = Path(file_path)
        ext = path.suffix.lower()
        items = None
        method = "regex_fallback"
        text = ""

        # For images and PDFs: try direct Gemini Vision → JSON first (single-step, captures everything)
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            items = self._parse_receipt_directly_from_image(file_path)
            if items:
                logger.info("Used direct Gemini Vision parsing for image (%d items)", len(items))
                method = "gemini_vision"
        elif ext == ".pdf" and self.ocr_available:
            # Convert first PDF page to image and try direct Vision parsing
            items = self._parse_receipt_directly_from_pdf_image(file_path)
            if items:
                logger.info("Used direct Gemini Vision parsing for PDF (%d items)", len(items))
                method = "gemini_vision"

        # Fallback: extract text then parse with Gemini (text-only)
        if not items:
            text, success, error_msg = self.extract_text(file_path)
            if not success:
                return [], False, error_msg, "text_extraction_failed"
            items = self._parse_receipt_with_gemini(text)
            if items:
                method = "gemini_text"

        # Fallback: table extraction for PDFs (pdfplumber)
        if not items and ext == ".pdf":
            items = self._extract_items_from_tables(file_path)
            if items:
                method = "pdfplumber_tables"

        # Last resort: regex on extracted text
        if not items:
            amounts = self._extract_totals_from_text(text)
            if amounts:
                date_str = datetime.now().strftime("%d/%m/%Y")
                items = [{
                    "Date": date_str,
                    "Item": "Receipt Total",
                    "Price": amounts[0],
                    "Qty": 1,
                    "Total": amounts[0],
                }]
                method = "regex_fallback"
            else:
                return [], False, (
                    "Unable to extract amounts from the receipt due to quality or format issues. "
                    "Please ensure the receipt is clear and contains recognizable price/total information."
                ), "failed"

        # Ensure Date on all items
        date_str = datetime.now().strftime("%d/%m/%Y")
        for item in items:
            if "Date" not in item or not item["Date"]:
                item["Date"] = date_str

        return items, True, None, method
