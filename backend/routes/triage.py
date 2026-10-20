import json
import re
import io
import os
import asyncio
from typing import List, Dict, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from google.genai import types
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


def _configured_models(env_name: str, defaults: List[str]) -> List[str]:
    """Return a de-duplicated model preference list from configuration."""
    configured = os.getenv(env_name, "")
    candidates = configured.split(",") if configured else defaults
    return list(dict.fromkeys(model.strip() for model in candidates if model.strip()))


def _gemini_history(history: List[TriageMessage]) -> List[types.Content]:
    """Build valid Gemini history, omitting the UI-only greeting."""
    normalized: List[types.Content] = []
    for message in history:
        content = message.content.strip()
        if not content:
            continue
        role = "user" if message.role == "user" else "model"
        if normalized and normalized[-1].role == role:
            previous = normalized[-1].parts[0].text or ""
            normalized[-1] = types.Content(
                role=role,
                parts=[types.Part(text=f"{previous}\n{content}".strip())],
            )
        else:
            normalized.append(types.Content(role=role, parts=[types.Part(text=content)]))

    while normalized and normalized[0].role != "user":
        normalized.pop(0)
    return normalized


def _local_triage_response(req: TriageRequest) -> dict:
    """Return a conservative, non-diagnostic response if AI providers fail."""
    user_history = [h.content for h in req.history if h.role == "user"]
    combined = " ".join([*user_history, req.message]).lower()
    emergency_markers = (
        "chest pain", "can't breathe", "cannot breathe", "difficulty breathing",
        "severe bleeding", "unconscious", "passed out", "face drooping",
        "slurred speech", "seizure", "anaphylaxis", "suicidal",
    )
    if any(marker in combined for marker in emergency_markers):
        return {
            "response": (
                "Your symptoms may need emergency care. Call local emergency services now "
                "(112 or 108 in India), and do not drive yourself. If someone is nearby, ask them to stay with you."
            ),
            "triage_level": "Emergency",
            "symptom_summary": req.message.strip(),
            "recommended_action": "Call emergency services now.",
            "care_recommendations": [
                "Call 112 or 108 in India, or your local emergency number.",
                "Do not drive yourself; ask someone to stay with you.",
                "Follow the emergency operator's instructions.",
            ],
            "medication_reminders": [],
            "session_complete": True,
            "ai_status": "safe_fallback",
        }

    if "fever" in combined or "temperature" in combined:
        response = (
            "I can help assess the urgency. What is your measured temperature, how long has the fever lasted, "
            "and do you have trouble breathing, confusion, a stiff neck, a seizure, or a new rash?"
        )
        recommendations = [
            "Rest and drink fluids if you can keep them down.",
            "Measure and note your temperature.",
            "Seek urgent help now for breathing difficulty, confusion, seizure, severe dehydration, or a stiff neck.",
        ]
    elif not any(token in combined for token in ("started", "since", "hour", "day", "week")):
        response = "When did this start, and was the onset sudden or gradual?"
        recommendations = ["Note when the symptom began and whether it is worsening."]
    else:
        response = "On a scale of 1 to 10, how severe is it, and are you having any other symptoms?"
        recommendations = ["Monitor for rapid worsening or any new emergency warning signs."]

    return {
        "response": response,
        "triage_level": "Assessing",
        "symptom_summary": " ".join([*user_history, req.message]).strip(),
        "recommended_action": "Answer the follow-up question so urgency can be assessed.",
        "care_recommendations": recommendations,
        "medication_reminders": [],
        "session_complete": False,
        "ai_status": "safe_fallback",
    }

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

    response_text = None
    provider = None
    new_sdk_history = _gemini_history(req.history)

    try:
        client = get_gemini_client()
    except Exception as e:
        print(f"Gemini client unavailable: {e}")
        client = None

    if client:
        for model in _configured_models(
            "GEMINI_CHAT_MODELS",
            ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash"],
        ):
            try:
                chat_session = client.chats.create(
                    model=model,
                    history=new_sdk_history,
                    config=types.GenerateContentConfig(
                        system_instruction=TRIAGE_SYSTEM_PROMPT,
                        temperature=0.2,
                        response_mime_type="application/json",
                        max_output_tokens=1500,
                    ),
                )
                result = await asyncio.to_thread(chat_session.send_message, req.message)
                response_text = result.text
                provider = f"gemini:{model}"
                break
            except Exception as e:
                print(f"Gemini model {model} unavailable ({getattr(e, 'code', 'error')}): {e}")

    if not response_text:
        groq_client = get_groq_client()
        if groq_client:
            messages = [{"role": "system", "content": TRIAGE_SYSTEM_PROMPT}]
            for h in req.history:
                if h.content.strip():
                    messages.append({
                        "role": "user" if h.role == "user" else "assistant",
                        "content": h.content,
                    })
            messages.append({"role": "user", "content": req.message})
            for model in _configured_models(
                "GROQ_CHAT_MODELS",
                ["openai/gpt-oss-20b", "llama-3.1-8b-instant"],
            ):
                try:
                    completion = await asyncio.to_thread(
                        groq_client.chat.completions.create,
                        model=model,
                        messages=messages,
                        temperature=0.2,
                        response_format={"type": "json_object"},
                    )
                    response_text = completion.choices[0].message.content
                    provider = f"groq:{model}"
                    break
                except Exception as e:
                    print(f"Groq model {model} unavailable: {e}")

    if response_text:
        try:
            response_json = json.loads(re.sub(r"```json\n?|\n?```", "", response_text).strip())
        except Exception:
            json_match = re.search(r"\{[\s\S]*\}", response_text)
            try:
                response_json = json.loads(json_match.group(0)) if json_match else {}
            except Exception:
                response_json = {}
        if not isinstance(response_json, dict) or not response_json.get("response"):
            response_json = _local_triage_response(req)
        else:
            response_json["ai_status"] = "available"
            response_json["ai_provider"] = provider
    else:
        print("All configured AI providers failed; using safe local triage fallback.")
        response_json = _local_triage_response(req)

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
