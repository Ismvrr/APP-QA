"""Protected monthly report endpoints."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.database import get_db
from app.services.jwt import get_current_user
from app.services.reports import _result_text, markdown_to_html, render_monthly_pdf

router = APIRouter(prefix="/api/reports", tags=["reports"])
logger = logging.getLogger(__name__)


def _pdf_response(pdf: bytes, filename: str) -> Response:
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/monthly")
async def monthly_report(
    company_id: int,
    year: int,
    month: int,
    user: dict = Depends(get_current_user),
):
    token_company_id = user.get("company_id")
    if token_company_id is not None and int(token_company_id) != company_id:
        raise HTTPException(status_code=403, detail="Company access denied")
    if not 2020 <= year <= 2030 or not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="Invalid report period")

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT name FROM companies WHERE id=%s AND isdeleted=0""",
                (company_id,),
            )
            company = cursor.fetchone()
            if not company:
                raise HTTPException(status_code=404, detail="Company not found")

            cursor.execute(
                """SELECT completed_at FROM extracted_periods
                   WHERE company_id=%s AND year=%s AND month=%s AND status='completed'""",
                (company_id, year, month),
            )
            extraction = cursor.fetchone()
            if not extraction or not extraction["completed_at"]:
                raise HTTPException(status_code=404, detail="Period has not been extracted")

            cursor.execute(
                """SELECT id, started_at, consolidation_result
                   FROM analysis_batches
                   WHERE company_id=%s AND year=%s AND month=%s AND status='completed'
                   ORDER BY id DESC LIMIT 1""",
                (company_id, year, month),
            )
            batch = cursor.fetchone()
            if not batch:
                raise HTTPException(status_code=404, detail="No completed analysis batch for this period")

            cursor.execute(
                """SELECT id, prompt1_result, prompt2_result, status,
                          gemini_key_source, gemini_tokens_used,
                          input_tokens, output_tokens, completed_at
                   FROM analysis_jobs
                   WHERE company_id=%s AND year=%s AND month=%s
                     AND status='completed'
                     AND (
                         analysis_batch_id=%s
                         OR (analysis_batch_id IS NULL AND prompt2_result IS NOT NULL
                             AND started_at >= %s)
                     )
                   ORDER BY id ASC""",
                (company_id, year, month, batch["id"], batch["started_at"]),
            )
            jobs = cursor.fetchall()

    if not jobs:
        raise HTTPException(status_code=404, detail="No completed analysis for this period")

    details = [
        {
            "id": job["id"],
            "html": markdown_to_html(_result_text(job["prompt1_result"])),
        }
        for job in jobs
        if job["prompt1_result"]
    ]
    consolidations = [job for job in jobs if job["prompt2_result"]]
    consolidation = _result_text(consolidations[-1]["prompt2_result"]) if consolidations else ""
    if not consolidation and batch.get("consolidation_result"):
        consolidation = _result_text(batch["consolidation_result"])
    total_tokens = sum(job["gemini_tokens_used"] or 0 for job in jobs)

    pdf = render_monthly_pdf({
        "company_name": company["name"],
        "year": year,
        "month": month,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "details": details,
        "consolidation_html": markdown_to_html(consolidation),
    })
    filename = f"reporte-{company['name']}-{year}-{month:02d}.pdf".replace(" ", "-")
    return _pdf_response(pdf, filename)


@router.get("/job/{job_id}")
async def analysis_job_report(
    job_id: int,
    company_id: int,
    user: dict = Depends(get_current_user),
):
    """Generates a PDF for one historical completed analysis job."""
    token_company_id = user.get("company_id")
    if token_company_id is not None and int(token_company_id) != company_id:
        raise HTTPException(status_code=403, detail="Company access denied")

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT c.name, j.id, j.year, j.month, j.prompt1_result,
                          j.prompt2_result, j.gemini_tokens_used
                   FROM analysis_jobs j
                   JOIN companies c ON c.id=j.company_id
                   WHERE j.id=%s AND j.company_id=%s AND j.status='completed'""",
                (job_id, company_id),
            )
            job = cursor.fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Completed analysis job not found")

    report = {
        "company_name": job["name"],
        "year": job["year"],
        "month": job["month"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "details": [],
        "consolidation_html": markdown_to_html(_result_text(job["prompt2_result"])),
    }
    if job["prompt1_result"]:
        report["details"].append({
            "id": job["id"],
            "html": markdown_to_html(_result_text(job["prompt1_result"])),
        })

    pdf = render_monthly_pdf(report)
    return _pdf_response(pdf, f"reporte-job-{job_id}.pdf")
