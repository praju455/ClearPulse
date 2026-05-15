
import hashlib
import json
import re
import base64
import logging
import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.gemini import get_gemini_client
from services.groq_client import get_groq_client
from services.insforge import db_insert
from google.genai import types
from google.genai.errors import ClientError

logger = logging.getLogger(__name__)

router = APIRouter()


class AnalyzeRequest(BaseModel):
    file_base64: str
    file_type: str = "application/pdf"
    patient_wallet: str
    file_name: str = "report"


def _fallback_analysis_from_text(file_bytes: bytes, file_type: str) -> dict:
    text = ""
    if file_type.startswith("text/"):
        text = file_bytes.decode("utf-8", errors="ignore").strip()

    preview = re.sub(r"\s+", " ", text)[:900]
    summary = (
        "The AI analysis service is temporarily overloaded, so MediChain saved a basic review placeholder for this upload. "
        "Please retry analysis later for a full medical summary. "
        "Do not use this placeholder as medical advice."
    )
    if preview:
        summary += f" Extracted report preview: {preview}"

    return {
        "summary": summary,
        "risk_score": 50,
        "conditions": ["AI analysis temporarily unavailable"],
        "biomarkers": {},
        "specialist": "General Practitioner",
        "urgency": "medium",
        "improvement_plan": [
            "Retry the AI analysis in a few minutes.",
            "Share urgent or concerning results with a qualified clinician.",
            "Keep the original report available for medical review.",
        ],
    }


async def _generate_analysis_with_retries(client, prompt: str, file_bytes: bytes, file_type: str):
    # Updated to valid model names and fewer retries to fail over faster
    models = ["gemini-2.5-flash", "gemini-2.0-flash"]
    last_error = None

    for model in models:
        for attempt in range(1): # Reduced to 1 attempt per model for speed
            try:
                return client.models.generate_content(
                    model=model,
                    contents=[
                        prompt,
                        types.Part.from_bytes(data=file_bytes, mime_type=file_type),
                    ],
                )
            except ClientError as e:
                last_error = e
                status_code = getattr(e, "code", 500)
                error_msg = str(getattr(e, 'message', str(e))).lower()
                # Always fall through to Groq — don't re-raise for location/quota/model errors
                if status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(0.5)
                elif 'location' in error_msg or 'not supported' in error_msg or status_code == 400:
                    logger.warning(f"[Gemini] Skipping model {model}: {error_msg}")
                else:
                    logger.warning(f"[Gemini] ClientError {status_code} on {model}: {error_msg}")
                continue
            except Exception as e:
                last_error = e
                logger.warning(f"[Gemini] Unexpected error on {model}: {e}")
                await asyncio.sleep(0.5)

    # Groq Fallback
    groq_client = get_groq_client()
    if groq_client:
        logger.warning("Gemini failed after retries. Falling back to Groq...")
        try:
            # Handle PDF text extraction if it's a PDF
            extracted_text = ""
            if file_type == "application/pdf":
                try:
                    import io
                    from pypdf import PdfReader
                    reader = PdfReader(io.BytesIO(file_bytes))
                    extracted_text = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
                except Exception as pdf_e:
                    logger.error(f"PDF text extraction failed: {pdf_e}")

            # Choose model based on content
            is_image = file_type.startswith("image/")
            
            if is_image:
                # Use Groq Vision for images
                b64_data = base64.b64encode(file_bytes).decode("utf-8")
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{file_type};base64,{b64_data}"
                                }
                            }
                        ]
                    }
                ]
                model = "llama-3.2-11b-vision-preview"
            else:
                # Use text-only LLM for PDFs (with extracted text) or text files
                content = prompt
                if extracted_text:
                    content += f"\n\n--- EXTRACTED REPORT TEXT ---\n{extracted_text}"
                elif file_type.startswith("text/"):
                    content += f"\n\n--- REPORT TEXT ---\n{file_bytes.decode('utf-8', errors='ignore')}"
                
                messages = [
                    {"role": "user", "content": content}
                ]
                model = "llama-3.3-70b-versatile"

            completion = groq_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            
            class MockResult:
                text = completion.choices[0].message.content
            return MockResult()
            
        except Exception as groq_e:
            logger.error(f"Groq fallback failed: {groq_e}")

    raise last_error or RuntimeError("AI service unavailable")


@router.post("/analyze-report")
async def analyze_report(req: AnalyzeRequest):
    if not req.file_base64 or not req.patient_wallet:
        raise HTTPException(status_code=400, detail="file_base64 and patient_wallet are required")

    client = get_gemini_client()

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

    used_ai_fallback = False

    try:
        response = await _generate_analysis_with_retries(client, prompt, file_bytes, req.file_type)
    except ClientError as e:
        status_code = getattr(e, "code", 500)
        error_msg = str(getattr(e, 'message', str(e)))
        logger.warning("Gemini error (using fallback): [%s] %s", status_code, error_msg)
        response = None
        used_ai_fallback = True
    except Exception as e:
        logger.warning("Failed to communicate with AI service (using fallback): %s", e)
        response = None
        used_ai_fallback = True

    response_text = response.text if response is not None else ""

    if used_ai_fallback:
        analysis = _fallback_analysis_from_text(file_bytes, req.file_type)
    else:
        # Parse JSON safely
        try:
            cleaned = re.sub(r"```json\n?|\n?```", "", response_text).strip()
            json_match = re.search(r"\{[\s\S]*\}", cleaned)
            analysis = json.loads(json_match.group(0) if json_match else cleaned)
        except Exception:
            analysis = {
                "summary": "Analysis completed but format was invalid. Please review the raw report manually.",
                "risk_score": 50,
                "conditions": ["Formatted extraction failed"],
                "biomarkers": {},
                "specialist": "General Practitioner",
                "urgency": "low",
                "improvement_plan": ["Please consult a healthcare professional for a tailored improvement plan."]
            }

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
        "ai_status": "temporarily_unavailable" if used_ai_fallback else "available",
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
