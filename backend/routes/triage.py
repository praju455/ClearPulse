import json
import re
import io
from typing import List, Dict, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from google.genai import types
from google.genai.errors import ClientError
from services.gemini import get_gemini_client
from services.groq_client import get_groq_client
from services.insforge import db_select, db_insert
from services.sarvam import transcribe_audio, speak_text, is_sarvam_configured, SUPPORTED_LANGUAGES

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────

class TriageMessage(BaseModel):
    role: str
    content: str

class TriageRequest(BaseModel):
    patient_id: str
    message: str
    history: List[TriageMessage] = []
    language_code: str = "en-IN"

class TTSRequest(BaseModel):
    text: str
    language_code: str = "en-IN"

# ─────────────────────────────────────────────────────────────────────────────
# System Prompt (Enhanced NLP — Structured Symptom Collection + Care Engine)
# ─────────────────────────────────────────────────────────────────────────────

TRIAGE_SYSTEM_PROMPT = """You are ClearPulse — an AI-powered Emergency Triage & Care Recommendation Assistant for patients in areas with limited healthcare access.

YOUR MISSION: Conduct a structured, empathetic medical intake conversation. Collect enough information to triage the patient and recommend a care plan.

STRUCTURED INTAKE PROTOCOL (collect these progressively, not all at once):
1. Chief complaint — What is the main symptom?
2. Onset — When did it start? Sudden or gradual?
3. Severity — Rate pain/discomfort 1-10
4. Location & radiation — Where exactly? Does it spread?
5. Associated symptoms — Any other symptoms (fever, nausea, dizziness, shortness of breath)?
6. Medical history — Any known conditions, allergies, or surgeries?
7. Current medications — Any medicines being taken?
8. Risk factors — Age group, smoking, diabetes, pregnancy, etc.?

TRIAGE LEVELS:
- "Home": Mild, self-limiting. Manage with OTC meds, rest, hydration.
- "Clinic": Needs professional evaluation within 1-3 days. Not immediately life-threatening.
- "Emergency": Potentially life-threatening. Needs immediate emergency care. Signs: chest pain, stroke symptoms, severe bleeding, anaphylaxis, unconsciousness, difficulty breathing.
- "Assessing": Still collecting information.

CARE RECOMMENDATION ENGINE: Once you have enough context, generate a specific care_recommendations list:
- For Home: specific OTC medicines, home remedies, red flag symptoms to watch for
- For Clinic: type of specialist to see, tests likely needed, what to tell the doctor
- For Emergency: immediate actions to take right now (call 108, specific first aid)

MEDICATION REMINDER ENGINE:
- If the patient mentions medicines they already take, or if you recommend safe OTC/supportive medicines, create medication_reminders.
- Do not invent prescription medicines, dosages, or durations that were not stated by the patient or appropriate for common OTC guidance.
- Each reminder should include simple timing suggestions and a safety note.
- If there is not enough medication context, return an empty medication_reminders array.

RESPONSE FORMAT — Always respond as strict JSON:
{
  "response": "Your conversational message to the patient. Be warm, clear, and in simple language. Ask ONE focused follow-up question at a time.",
  "triage_level": "Home" | "Clinic" | "Emergency" | "Assessing",
  "symptom_summary": "A one-paragraph clinical summary of all symptoms collected so far. Empty string if still in early assessment.",
  "recommended_action": "Single most important action the patient should take right now.",
  "care_recommendations": ["Specific actionable step 1", "Specific actionable step 2", "..."],
  "medication_reminders": [
    {
      "medication_name": "Medicine name",
      "dosage": "Dose if known, otherwise 'As directed'",
      "instructions": "How to take it safely",
      "times": ["09:00", "21:00"],
      "frequency": "Once daily | Twice daily | Every 6 hours | As needed",
      "duration_days": 3,
      "safety_note": "Short warning about allergies, pregnancy, interactions, overdose, or when to ask a doctor"
    }
  ],
  "session_complete": false
}

Set session_complete to true only when you have gathered enough information to give a final triage decision.

IMPORTANT RULES:
- Never diagnose. Triage and recommend — never replace a doctor.
- Be culturally sensitive and use simple language.
- If the patient describes emergency symptoms at any point, immediately escalate to Emergency level.
- Always end your response with the medical disclaimer only once, when session_complete is true.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Helper: save triage session to DB
# ─────────────────────────────────────────────────────────────────────────────

async def _save_triage_session(patient_id: str, response_json: dict, message: str):
    """Save completed or in-progress triage session for doctor dashboard visibility."""
    try:
        if response_json.get("triage_level") not in ("Assessing",) or response_json.get("session_complete"):
            await db_insert("triage_sessions", {
                "patient_id": patient_id,
                "triage_level": response_json.get("triage_level", "Assessing"),
                "symptom_summary": response_json.get("symptom_summary", ""),
                "recommended_action": response_json.get("recommended_action", ""),
                "care_recommendations": json.dumps(response_json.get("care_recommendations", [])),
                "medication_reminders": json.dumps(response_json.get("medication_reminders", [])),
                "session_complete": response_json.get("session_complete", False),
                "last_message": message,
            })
    except Exception as e:
        print(f"[DB] Triage session save failed (non-critical): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Route: POST /api/triage/chat
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/chat")
async def triage_chat(req: TriageRequest):
    if not req.patient_id or not req.message:
        raise HTTPException(status_code=400, detail="patient_id and message are required")

    client = get_gemini_client()

    # Convert history to Gemini format
    new_sdk_history = []
    for h in req.history:
        role = "user" if h.role == "user" else "model"
        new_sdk_history.append(
            types.Content(role=role, parts=[types.Part(text=h.content)])
        )

    chat_session = client.chats.create(
        model="gemini-2.5-flash",
        history=new_sdk_history,
        config=types.GenerateContentConfig(
            system_instruction=TRIAGE_SYSTEM_PROMPT,
            temperature=0.2,
            response_mime_type="application/json",
            max_output_tokens=1500,
        ),
    )

    response_text = None
    try:
        result = chat_session.send_message(req.message)
        response_text = result.text
    except Exception as e:
        status_code = getattr(e, "code", 500)
        if status_code in (429, 500, 502, 503, 504) or isinstance(e, ClientError):
            print(f"Gemini unavailable ({status_code}). Falling back to Groq...")
            groq_client = get_groq_client()
            if groq_client:
                messages = [{"role": "system", "content": TRIAGE_SYSTEM_PROMPT}]
                for h in req.history:
                    messages.append({"role": "user" if h.role == "user" else "assistant", "content": h.content})
                messages.append({"role": "user", "content": req.message})
                try:
                    completion = groq_client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=messages,
                        temperature=0.2,
                        response_format={"type": "json_object"},
                    )
                    response_text = completion.choices[0].message.content
                except Exception as groq_e:
                    print(f"Groq fallback also failed: {groq_e}")
                    raise HTTPException(status_code=500, detail="Failed to process triage request (both AI services down).")
            else:
                raise HTTPException(status_code=500, detail="Gemini failed and Groq API key is not configured.")
        else:
            print(f"Error calling Gemini: {e}")
            raise HTTPException(status_code=500, detail="Failed to process triage request.")

    if not response_text:
        raise HTTPException(status_code=500, detail="Failed to get valid response from AI.")

    # Parse JSON
    try:
        response_json = json.loads(re.sub(r"```json\n?|\n?```", "", response_text).strip())
    except Exception:
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            try:
                response_json = json.loads(json_match.group(0))
            except Exception:
                response_json = {}
        else:
            response_json = {}

    # Ensure all keys exist
    response_json.setdefault("response", "I'm having trouble understanding. Could you describe your symptoms again?")
    response_json.setdefault("triage_level", "Assessing")
    response_json.setdefault("symptom_summary", "")
    response_json.setdefault("recommended_action", "")
    response_json.setdefault("care_recommendations", [])
    response_json.setdefault("medication_reminders", [])
    response_json.setdefault("session_complete", False)

    # Save triage history messages
    try:
        await db_insert("triage_history", {
            "patient_id": req.patient_id,
            "role": "user",
            "message": req.message
        })
        await db_insert("triage_history", {
            "patient_id": req.patient_id,
            "role": "assistant",
            "message": response_json["response"],
            "triage_level": response_json["triage_level"],
            "recommended_action": response_json["recommended_action"]
        })
    except Exception as e:
        print(f"[DB] Optional triage history save failed: {e}")

    # Save session summary for doctor dashboard
    await _save_triage_session(req.patient_id, response_json, req.message)

    return response_json


# ─────────────────────────────────────────────────────────────────────────────
# Route: POST /api/triage/stt  — Speech-to-Text via Sarvam AI
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/stt")
async def triage_stt(
    audio: UploadFile = File(...),
    language_code: str = Form(default="hi-IN"),
):
    if not is_sarvam_configured():
        raise HTTPException(
            status_code=503,
            detail="Sarvam AI STT is not configured. Please set SARVAM_API_KEY in your .env file."
        )

    try:
        audio_bytes = await audio.read()
        transcript = await transcribe_audio(audio_bytes, language_code, audio.filename or "audio.wav")
        return {"transcript": transcript, "language_code": language_code}
    except Exception as e:
        print(f"Sarvam STT error: {e}")
        raise HTTPException(status_code=500, detail=f"STT failed: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# Route: POST /api/triage/tts  — Text-to-Speech via Sarvam AI
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/tts")
async def triage_tts(req: TTSRequest):
    if not is_sarvam_configured():
        raise HTTPException(
            status_code=503,
            detail="Sarvam AI TTS is not configured. Please set SARVAM_API_KEY in your .env file."
        )

    try:
        audio_bytes = await speak_text(req.text, req.language_code)
        return StreamingResponse(
            io.BytesIO(audio_bytes),
            media_type="audio/wav",
            headers={"Content-Disposition": "inline; filename=response.wav"}
        )
    except Exception as e:
        print(f"Sarvam TTS error: {e}")
        raise HTTPException(status_code=500, detail=f"TTS failed: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /api/triage/sessions/{patient_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sessions/{patient_id}")
async def get_triage_sessions(patient_id: str):
    try:
        result = await db_select("triage_sessions", filters={"patient_id": patient_id}, order="created_at.desc")
        sessions = result if isinstance(result, list) else []
        # Parse care_recommendations JSON string back to list
        for s in sessions:
            if isinstance(s.get("care_recommendations"), str):
                try:
                    s["care_recommendations"] = json.loads(s["care_recommendations"])
                except Exception:
                    s["care_recommendations"] = []
            if isinstance(s.get("medication_reminders"), str):
                try:
                    s["medication_reminders"] = json.loads(s["medication_reminders"])
                except Exception:
                    s["medication_reminders"] = []
        return {"sessions": sessions}
    except Exception as e:
        print(f"[DB] Triage sessions fetch failed: {e}")
        return {"sessions": [], "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /api/triage/doctor-alerts/{doctor_id}
# Returns Clinic/Emergency triage sessions from patients visible to this doctor
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/doctor-alerts/{doctor_id}")
async def get_doctor_triage_alerts(doctor_id: str):
    try:
        # Get patients who have granted access to this doctor
        grants_result = await db_select(
            "doctor_access_grants",
            filters={"doctor_wallet": doctor_id}
        )
        granted_patients = set()
        if isinstance(grants_result, list):
            for g in grants_result:
                if g.get("patient_wallet"):
                    granted_patients.add(g["patient_wallet"])

        # Also get patients with appointments
        appt_result = await db_select(
            "appointments",
            filters={"doctor_wallet": doctor_id}
        )
        if isinstance(appt_result, list):
            for a in appt_result:
                if a.get("patient_wallet"):
                    granted_patients.add(a["patient_wallet"])

        # Fetch Clinic/Emergency sessions for all visible patients
        all_alerts = []
        for patient_id in granted_patients:
            sessions = await db_select(
                "triage_sessions",
                filters={"patient_id": patient_id},
                order="created_at.desc"
            )
            if isinstance(sessions, list):
                for s in sessions:
                    if s.get("triage_level") in ("Clinic", "Emergency"):
                        if isinstance(s.get("care_recommendations"), str):
                            try:
                                s["care_recommendations"] = json.loads(s["care_recommendations"])
                            except Exception:
                                s["care_recommendations"] = []
                        if isinstance(s.get("medication_reminders"), str):
                            try:
                                s["medication_reminders"] = json.loads(s["medication_reminders"])
                            except Exception:
                                s["medication_reminders"] = []
                        all_alerts.append(s)

        # Sort by severity (Emergency first) then by time
        severity_order = {"Emergency": 0, "Clinic": 1, "Home": 2, "Assessing": 3}
        all_alerts.sort(key=lambda x: (severity_order.get(x.get("triage_level", "Assessing"), 3), x.get("created_at", "")))

        return {"alerts": all_alerts, "total": len(all_alerts)}

    except Exception as e:
        print(f"[DB] Doctor alerts fetch failed: {e}")
        return {"alerts": [], "total": 0, "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /api/triage/languages — List supported languages
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/languages")
async def get_supported_languages():
    return {
        "languages": [{"code": k, "name": v} for k, v in SUPPORTED_LANGUAGES.items()],
        "sarvam_configured": is_sarvam_configured()
    }
