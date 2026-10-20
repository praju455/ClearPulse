import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.gemini import get_gemini_client
from services.groq_client import get_groq_client
from services.insforge import db_insert
from google.genai import types

logger = logging.getLogger(__name__)

router = APIRouter()


class AnalyzeRequest(BaseModel):
    file_base64: str
    file_type: str = "application/pdf"
    patient_wallet: str
    file_name: str = "report"


def _configured_models(env_name: str, defaults: list[str]) -> list[str]:
    configured = os.getenv(env_name, "")
    candidates = configured.split(",") if configured else defaults
    return list(dict.fromkeys(model.strip() for model in candidates if model.strip()))


def _extract_report_text(file_bytes: bytes, file_type: str) -> str:
    if file_type.startswith("text/"):
        return file_bytes.decode("utf-8", errors="ignore").strip()
    if file_type == "application/pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        except Exception as error:
            logger.warning("PDF text extraction failed: %s", error)
    return ""


async def _generate_analysis_with_retries(client, prompt: str, file_bytes: bytes, file_type: str):
    last_error = None

    if client:
        for model in _configured_models(
            "GEMINI_ANALYSIS_MODELS",
            ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash"],
        ):
            try:
                return await asyncio.to_thread(
                    client.models.generate_content,
                    model=model,
                    contents=[prompt, types.Part.from_bytes(data=file_bytes, mime_type=file_type)],
                )
            except Exception as e:
                last_error = e
                logger.warning("[Gemini] Model %s failed: %s", model, e)

    # Groq Fallback
    groq_client = get_groq_client()
    if groq_client:
        logger.warning("Gemini failed after retries. Falling back to Groq...")
        extracted_text = _extract_report_text(file_bytes, file_type)
        if extracted_text:
            content = f"{prompt}\n\n--- EXTRACTED REPORT TEXT ---\n{extracted_text}"
            for model in _configured_models(
                "GROQ_ANALYSIS_MODELS",
                ["openai/gpt-oss-20b", "llama-3.1-8b-instant"],
            ):
                try:
                    completion = await asyncio.to_thread(
                        groq_client.chat.completions.create,
                        model=model,
                        messages=[{"role": "user", "content": content}],
                        temperature=0.2,
                        response_format={"type": "json_object"},
                    )

                    class MockResult:
                        text = completion.choices[0].message.content

                    return MockResult()
                except Exception as groq_error:
                    last_error = groq_error
                    logger.warning("[Groq] Model %s failed: %s", model, groq_error)
        else:
            logger.warning("Groq fallback skipped because no text could be extracted from the report.")

    raise last_error or RuntimeError("AI service unavailable")


@router.post("/analyze-report")
async def analyze_report(req: AnalyzeRequest):
    if not req.file_base64 or not req.patient_wallet:
        raise HTTPException(status_code=400, detail="file_base64 and patient_wallet are required")

    prompt = """You are a professional medical analysis AI assistant. Analyze the provided medical report and return a structured JSON response.
You are NOT replacing a doctor.
Do not include any personal information in the JSON response.
Return ONLY valid JSON in this exact format:
{
  "summary": "A detailed, empathetic summary (4-6 sentences) focusing on the patient's experience, symptoms, and potential impact on daily life.",
  "risk_score": 50,
  "conditions": ["list", "of", "detected", "conditions"],
  "biomarkers": { "hemoglobin": "12.5 g/dL", "platelets": "250000 /uL" },
  "specialist": "Recommended specialist type (e.g. Cardiologist)",
  "urgency": "low|medium|high|critical",
  "improvement_plan": ["Actionable lifestyle/health step 1", "Actionable step 2", "Actionable step 3"]
}
Ensure risk_score is a number. Extract biomarkers as key-value pairs where possible."""

    file_bytes = base64.b64decode(req.file_base64)

    try:
        try:
            client = get_gemini_client()
        except Exception as client_error:
            logger.warning("Gemini client unavailable: %s", client_error)
            client = None
        response = await _generate_analysis_with_retries(client, prompt, file_bytes, req.file_type)
    except Exception as e:
        logger.error("All report-analysis providers failed: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Report analysis is temporarily unavailable. The report was not saved with a placeholder; please retry shortly.",
        )

    response_text = response.text or ""
    try:
        cleaned = re.sub(r"```json\n?|\n?```", "", response_text).strip()
        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        analysis = json.loads(json_match.group(0) if json_match else cleaned)
        if not isinstance(analysis, dict) or not analysis.get("summary"):
            raise ValueError("AI response did not contain a usable analysis")
    except Exception as parse_error:
        logger.error("Report analysis returned invalid JSON: %s", parse_error)
        raise HTTPException(
            status_code=502,
            detail="The analysis provider returned an invalid result. The report was not saved; please retry.",
        )

    # SHA-256 hash of the analysis result
    hash_payload = json.dumps(analysis).encode()
    record_hash = "0x" + hashlib.sha256(hash_payload).hexdigest()

    db_record = None
    service_status = "available"
    service_message = None

    try:
        # Store in InsForge DB when available. Analysis should still succeed if
        # the database provider is temporarily unavailable.
        db_record = await db_insert("analyses", {
            "patient_wallet": req.patient_wallet,
            "file_name": req.file_name,
            "file_url": "direct-upload",
            "ocr_text": response_text,
            "summary": analysis.get("summary", ""),
            "risk_score": analysis.get("risk_score", 50),
            "conditions": analysis.get("conditions", []),
            "biomarkers": analysis.get("biomarkers", {}),
            "specialist": analysis.get("specialist", "General"),
            "urgency": analysis.get("urgency", "low"),
            "improvement_plan": analysis.get("improvement_plan", []),
            "record_hash": record_hash,
        })
    except Exception as e:
        logger.warning("Analysis completed but database save failed: %s", e)
        service_status = "temporarily_unavailable"
        service_message = "Analysis completed, but record storage is temporarily unavailable."

    return {
        "success": True,
        "service_status": service_status,
        "message": service_message,
        "ai_status": "available",
        "analysis": {
            "id": db_record.get("id") if db_record else None,
            "file_name": req.file_name,
            "file_url": "direct-upload",
            "summary": analysis.get("summary"),
            "risk_score": analysis.get("risk_score"),
            "conditions": analysis.get("conditions"),
            "specialist": analysis.get("specialist"),
            "urgency": analysis.get("urgency"),
            "biomarkers": analysis.get("biomarkers"),
            "improvement_plan": analysis.get("improvement_plan", []),
            "record_hash": record_hash,
            "created_at": datetime.utcnow().isoformat(),
        }
    }
