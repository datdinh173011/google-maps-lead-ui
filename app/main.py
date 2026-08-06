from __future__ import annotations

import csv
import io
import os
import re
import secrets
import unicodedata
from typing import Any, Literal

import httpx
import xlsxwriter
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator

SCRAPER_BASE_URL = os.getenv("SCRAPER_BASE_URL", "http://google-maps-scraper:8080").rstrip("/")
APP_USERNAME = os.getenv("APP_USERNAME", "").strip()
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
REQUEST_TIMEOUT = float(os.getenv("SCRAPER_REQUEST_TIMEOUT", "30"))

security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Enable HTTP Basic Auth only when both environment variables are configured."""
    if not APP_USERNAME or not APP_PASSWORD:
        return

    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Basic"})

    username_ok = secrets.compare_digest(credentials.username, APP_USERNAME)
    password_ok = secrets.compare_digest(credentials.password, APP_PASSWORD)
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(
    title="Google Maps Lead Exporter",
    version="1.0.0",
    dependencies=[Depends(require_auth)],
)


class CreateJobRequest(BaseModel):
    industry: str = Field(min_length=2, max_length=150)
    area: str = Field(min_length=2, max_length=200)
    subareas: list[str] = Field(default_factory=list, max_length=50)
    depth: int = Field(default=1, ge=1, le=20)
    email: bool = False
    extra_reviews: bool = False
    max_time: int = Field(default=600, ge=180, le=3600, description="Seconds")

    @field_validator("industry", "area")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        value = " ".join(value.strip().split())
        if not value:
            raise ValueError("Không được để trống")
        return value

    @field_validator("subareas")
    @classmethod
    def clean_subareas(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = " ".join(value.strip().split())
            key = cleaned.casefold()
            if cleaned and key not in seen:
                result.append(cleaned)
                seen.add(key)
        return result


def build_keywords(industry: str, area: str, subareas: list[str]) -> list[str]:
    if not subareas:
        return [f"{industry} tại {area}"]

    keywords: list[str] = []
    for subarea in subareas:
        if area.casefold() in subarea.casefold():
            keywords.append(f"{industry} tại {subarea}")
        else:
            keywords.append(f"{industry} tại {subarea}, {area}")
    return keywords


async def scraper_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            response = await client.request(method, f"{SCRAPER_BASE_URL}{path}", **kwargs)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Không kết nối được scraper: {exc}") from exc

    if response.status_code >= 400:
        message = response.text.strip()[:1000] or response.reason_phrase
        raise HTTPException(status_code=502, detail=f"Scraper trả lỗi {response.status_code}: {message}")
    return response


def normalize_job(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("Data") or raw.get("data") or {}
    return {
        "id": raw.get("ID") or raw.get("id"),
        "name": raw.get("Name") or raw.get("name"),
        "status": (raw.get("Status") or raw.get("status") or "unknown").lower(),
        "date": raw.get("Date") or raw.get("date"),
        "keywords": data.get("keywords") or data.get("Keywords") or [],
        "data": data,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as file:
        return HTMLResponse(file.read())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "scraper": SCRAPER_BASE_URL}


@app.post("/api/jobs")
async def create_job(request: CreateJobRequest) -> dict[str, Any]:
    keywords = build_keywords(request.industry, request.area, request.subareas)
    payload = {
        "name": f"{request.industry} - {request.area}",
        "keywords": keywords,
        "lang": "vi",
        "zoom": 15,
        "lat": "",
        "lon": "",
        "fast_mode": False,
        "radius": 10000,
        "depth": request.depth,
        "email": request.email,
        "extra_reviews": request.extra_reviews,
        # API Web UI expects seconds and converts them to time.Duration internally.
        "max_time": request.max_time,
        "proxies": [],
    }

    response = await scraper_request("POST", "/api/v1/jobs", json=payload)
    body = response.json()
    job_id = body.get("id") or body.get("ID")
    if not job_id:
        raise HTTPException(status_code=502, detail="Scraper không trả về job ID")

    return {"id": job_id, "status": "pending", "keywords": keywords, "name": payload["name"]}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    response = await scraper_request("GET", f"/api/v1/jobs/{job_id}")
    return normalize_job(response.json())


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value)
    ascii_text = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:100] or "google-maps-leads"


def safe_csv_value(value: Any) -> str:
    text = "" if value is None else str(value)
    # Protect spreadsheet applications from CSV formula injection.
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def parse_csv(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=502, detail="File CSV của scraper không có header")
    rows = [{key: (value or "") for key, value in row.items()} for row in reader]
    return list(reader.fieldnames), rows


CLEAN_COLUMNS: list[tuple[str, str]] = [
    ("title", "Tên doanh nghiệp"),
    ("category", "Ngành nghề"),
    ("address", "Địa chỉ"),
    ("phone", "Điện thoại"),
    ("website", "Website"),
    ("emails", "Email"),
    ("review_count", "Số đánh giá"),
    ("review_rating", "Điểm đánh giá"),
    ("latitude", "Vĩ độ"),
    ("longitude", "Kinh độ"),
    ("link", "Google Maps"),
    ("place_id", "Place ID"),
    ("cid", "CID"),
]


def clean_email(value: str) -> str:
    emails = []
    seen = set()
    for email in re.split(r"[,;\s]+", value or ""):
        email = email.strip()
        key = email.casefold()
        if not email or "@" not in email or key in {"noemail@noemail.com", "example@example.com"}:
            continue
        if key not in seen:
            emails.append(email)
            seen.add(key)
    return ", ".join(emails)


def normalize_location(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    ascii_text = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    ascii_text = re.sub(r"\b(thanh pho|tp|tinh)\b", " ", ascii_text, flags=re.IGNORECASE)
    return " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", ascii_text).casefold().split())


def row_matches_area(row: dict[str, str], area: str) -> bool:
    target = normalize_location(area)
    if not target:
        return True
    haystack = normalize_location(" ".join([row.get("address", ""), row.get("complete_address", "")]))
    return target in haystack


def build_clean_rows(
    rows: list[dict[str, str]], area: str = "", filter_area: bool = True
) -> tuple[list[str], list[list[str]]]:
    headers = [label for _, label in CLEAN_COLUMNS]
    output: list[list[str]] = []
    seen: set[str] = set()

    for row in rows:
        if filter_area and area and not row_matches_area(row, area):
            continue

        place_id = row.get("place_id", "").strip()
        phone = re.sub(r"\D", "", row.get("phone", ""))
        fallback = f"{row.get('title', '').strip().casefold()}|{row.get('address', '').strip().casefold()}"
        dedupe_key = place_id or phone or fallback
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        values: list[str] = []
        for key, _ in CLEAN_COLUMNS:
            value = row.get(key, "")
            if key == "emails":
                value = clean_email(value)
            values.append(value)
        output.append(values)
    return headers, output


def build_full_rows(headers: list[str], rows: list[dict[str, str]]) -> tuple[list[str], list[list[str]]]:
    return headers, [[row.get(header, "") for header in headers] for row in rows]


def csv_bytes(headers: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([safe_csv_value(value) for value in row])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def xlsx_bytes(headers: list[str], rows: list[list[str]], sheet_name: str) -> bytes:
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True, "constant_memory": True})
    worksheet = workbook.add_worksheet(sheet_name[:31])

    header_format = workbook.add_format(
        {
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#155E75",
            "border": 1,
            "align": "center",
            "valign": "vcenter",
        }
    )
    text_format = workbook.add_format({"valign": "top"})
    number_format = workbook.add_format({"num_format": "0", "valign": "top"})
    decimal_format = workbook.add_format({"num_format": "0.0", "valign": "top"})
    link_format = workbook.get_default_url_format()

    for col, header in enumerate(headers):
        worksheet.write(0, col, header, header_format)

    numeric_headers = {"Số đánh giá", "review_count"}
    decimal_headers = {"Điểm đánh giá", "review_rating", "Vĩ độ", "Kinh độ", "latitude", "longitude"}
    link_headers = {"Website", "Google Maps", "website", "link"}

    widths = [len(str(header)) for header in headers]
    for row_index, row in enumerate(rows, start=1):
        for col_index, value in enumerate(row):
            header = headers[col_index]
            text = "" if value is None else str(value)
            widths[col_index] = min(max(widths[col_index], len(text[:80])), 45)

            if header in link_headers and text.startswith(("http://", "https://")):
                worksheet.write_url(row_index, col_index, text, link_format, text)
            elif header in numeric_headers:
                try:
                    worksheet.write_number(row_index, col_index, float(text), number_format)
                except (TypeError, ValueError):
                    worksheet.write(row_index, col_index, text, text_format)
            elif header in decimal_headers:
                try:
                    worksheet.write_number(row_index, col_index, float(text), decimal_format)
                except (TypeError, ValueError):
                    worksheet.write(row_index, col_index, text, text_format)
            else:
                worksheet.write(row_index, col_index, text, text_format)

    worksheet.freeze_panes(1, 0)
    worksheet.autofilter(0, 0, max(len(rows), 1), max(len(headers) - 1, 0))
    worksheet.set_row(0, 26)
    for col_index, width in enumerate(widths):
        worksheet.set_column(col_index, col_index, min(max(width + 2, 12), 45))

    workbook.close()
    return output.getvalue()


@app.get("/api/jobs/{job_id}/download")
async def download_result(
    job_id: str,
    format: Literal["csv", "xlsx"] = Query(default="xlsx"),
    mode: Literal["clean", "full"] = Query(default="clean"),
    filter_area: bool = Query(default=True),
) -> StreamingResponse:
    status_response = await scraper_request("GET", f"/api/v1/jobs/{job_id}")
    job = normalize_job(status_response.json())
    if job["status"] == "failed":
        raise HTTPException(status_code=409, detail="Job đã thất bại")

    response = await scraper_request("GET", f"/api/v1/jobs/{job_id}/download")
    raw_headers, raw_rows = parse_csv(response.content)
    if mode == "clean":
        job_name = job.get("name") or ""
        area_hint = job_name.rsplit(" - ", 1)[1] if " - " in job_name else ""
        headers, rows = build_clean_rows(raw_rows, area=area_hint, filter_area=filter_area)
        suffix = "gon"
        sheet_name = "Leads"
    else:
        headers, rows = build_full_rows(raw_headers, raw_rows)
        suffix = "day-du"
        sheet_name = "Raw Data"

    filename_base = slugify(job.get("name") or f"job-{job_id}")
    if format == "csv":
        content = csv_bytes(headers, rows)
        media_type = "text/csv; charset=utf-8"
        filename = f"{filename_base}-{suffix}.csv"
    else:
        content = xlsx_bytes(headers, rows, sheet_name)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"{filename_base}-{suffix}.xlsx"

    return StreamingResponse(
        io.BytesIO(content),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
