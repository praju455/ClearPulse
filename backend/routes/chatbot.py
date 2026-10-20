import asyncio
import json
import os
import re
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from google.genai import types
from services.gemini import get_gemini_client
from services.groq_client import get_groq_client
from services.insforge import db_insert, db_select

router = APIRouter()


class ChatRequest(BaseModel):
    patient_wallet: str
    message: str


def _configured_models(env_name: str, defaults: List[str]) -> List[str]:
    """Return a de-duplicated model preference list from configuration."""
    configured = os.getenv(env_name, "")
    candidates = configured.split(",") if configured else defaults
    return list(dict.fromkeys(model.strip() for model in candidates if model.strip()))


def _gemini_history(history: list[dict]) -> List[types.Content]:
    """Normalize stored history into alternating Gemini roles."""
    normalized: List[types.Content] = []
    for message in reversed(history or []):
        content = str(message.get("message", "")).strip()
        if not content:
            continue
        role = "user" if message.get("role") == "user" else "model"
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
    while normalized and normalized[-1].role != "model":
        normalized.pop()
    return normalized


def _analysis_context(analyses: list[dict]) -> str:
    """Format report context without trusting optional database field types."""
    if not analyses:
        return "No reports found."

    lines = []
    for index, analysis in enumerate(analyses):
        conditions = analysis.get("conditions") or []
        if isinstance(conditions, str):
            conditions_text = conditions
        elif isinstance(conditions, list):
            conditions_text = ",".join(str(item) for item in conditions)
        else:
            conditions_text = str(conditions)
        lines.append(
            f"[{index + 1}] {analysis.get('summary') or ''} | "
            f"Risk:{analysis.get('risk_score', 0)} | {conditions_text} | "
            f"{analysis.get('specialist') or ''} | {analysis.get('urgency') or ''}"
        )
    return "\n".join(lines)


def _local_chat_response(req: ChatRequest, analyses: list[dict]) -> dict:
    """Return useful, conservative guidance when external AI is unavailable."""
    lowered = req.message.strip().lower()
    emergency_markers = (
        "chest pain", "can't breathe", "cannot breathe", "difficulty breathing",
        "severe bleeding", "unconscious", "passed out", "face drooping",
        "slurred speech", "seizure", "anaphylaxis", "suicidal",
    )
    if any(marker in lowered for marker in emergency_markers):
        return {
            "answer": (
                "This may need emergency care. Call local emergency services now "
                "(112 or 108 in India), do not drive yourself, and ask someone nearby to stay with you."
            ),
            "warning": "This is safety guidance, not a diagnosis. Do not delay emergency care.",
            "confidence": 1.0,
            "ai_status": "safe_fallback",
        }

    if analyses and any(term in lowered for term in ("report", "result", "risk", "analysis")):
        latest = analyses[0]
        summary = str(latest.get("summary") or "No written summary is available.").strip()
        risk_score = latest.get("risk_score")
        urgency = str(latest.get("urgency") or "not specified").strip()
        risk_text = f" The recorded risk score is {risk_score}." if risk_score is not None else ""
        answer = (
            f"Your latest saved report summary says: {summary}{risk_text} "
            f"The recorded urgency is {urgency}. Please review these results with a qualified clinician, "
            "especially if your symptoms are new or worsening."
        )
    elif "fever" in lowered or "temperature" in lowered:
        answer = (
            "For a fever, note the measured temperature, how long it has lasted, and any other symptoms. "
            "Rest and drink fluids if you can. Seek urgent care for breathing difficulty, confusion, a seizure, "
            "a stiff neck, a new widespread rash, severe dehydration, or rapid worsening."
        )
    else:
        answer = (
            "I cannot safely generate a personalized answer right now. Please monitor when the symptom started, "
            "its severity, and whether it is worsening. Contact a healthcare professional for advice; call emergency "
            "services for severe or rapidly worsening symptoms."
        )

    return {
        "answer": answer,
        "warning": "Informational only; this does not replace medical advice, diagnosis, or treatment.",
        "confidence": 0.5,
        "ai_status": "safe_fallback",
    }


@router.post("/chat")
async def chat(req: ChatRequest):
    if not req.patient_wallet or not req.message.strip():
        raise HTTPException(status_code=400, detail="patient_wallet and message are required")

    analyses, history = await _fetch_context(req.patient_wallet)
    analysis_context = _analysis_context(analyses)

    system_instruction = f"""You are MediLock AI, an advanced, empathetic, and highly professional medical assistant.

CORE PERSONALITY & TONE:
- Empathetic and Reassuring: Always start with a warm, caring, and professional tone.
- Clear and Accessible: Explain complex medical terms using simple, everyday language.
- Structured and Organized: Use bullet points, bold text, and clear paragraphs.
- Objective yet Supportive: Provide factual insights from the reports without causing unnecessary panic.

STRICT SAFETY RULES:
1. NEVER diagnose a condition or prescribe medication.
2. ALWAYS include a clear disclaimer that your advice is informational only.
3. For high risk scores or emergencies, urgently recommend consulting a healthcare professional.
4. If a question is outside the reports scope, offer general health information but reiterate your limitations.

AVAILABLE PATIENT REPORTS:
{analysis_context}

Return your response STRICTLY as a JSON object:
{{
  "answer": "Your detailed Markdown response here",
  "warning": "Brief disclaimer or empty string",
  "confidence": 0.9
}}"""

    new_sdk_history = _gemini_history(history)
    response_text = None
    provider = None

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
                        system_instruction=system_instruction,
                        temperature=0.3,
                        response_mime_type="application/json",
                        max_output_tokens=1024,
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
            messages = [{"role": "system", "content": system_instruction}]
            for item in new_sdk_history:
                messages.append({
                    "role": "user" if item.role == "user" else "assistant",
                    "content": item.parts[0].text or "",
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
                        temperature=0.3,
                        response_format={"type": "json_object"},
                    )
                    response_text = completion.choices[0].message.content
                    provider = f"groq:{model}"
                    break
                except Exception as e:
                    print(f"Groq model {model} unavailable: {e}")

    if response_text:
        try:
            response = json.loads(re.sub(r"```json\n?|\n?```", "", response_text).strip())
        except Exception:
            json_match = re.search(r"\{[\s\S]*\}", response_text)
            try:
                response = json.loads(json_match.group(0)) if json_match else {}
            except Exception:
                response = {}
        if not isinstance(response, dict) or not response.get("answer"):
            response = _local_chat_response(req, analyses)
        else:
            response["ai_status"] = "available"
            response["ai_provider"] = provider
    else:
        print("All configured AI providers failed; using safe local dashboard-chat fallback.")
        response = _local_chat_response(req, analyses)

    response.setdefault("answer", "Please try describing your question again.")
    response.setdefault("warning", "Informational only; this does not replace medical advice.")
    response.setdefault("confidence", 0.5)

    try:
        await db_insert("chat_history", {
            "patient_wallet": req.patient_wallet,
            "role": "user",
            "message": req.message,
        })
        await db_insert("chat_history", {
            "patient_wallet": req.patient_wallet,
            "role": "assistant",
            "message": response.get("answer", ""),
            "warning": response.get("warning", ""),
            "confidence": response.get("confidence", 0.5),
        })
    except Exception as e:
        print(f"[DB] Failed to save chat: {e}")

    return response


async def _fetch_context(patient_wallet: str):
    try:
        analyses = await db_select(
            "analyses",
            filters={"patient_wallet": patient_wallet},
            select="summary,risk_score,conditions,specialist,urgency,created_at",
            order="created_at.desc",
            limit=5,
        )
    except Exception:
        analyses = []

    try:
        history = await db_select(
            "chat_history",
            filters={"patient_wallet": patient_wallet},
            select="role,message",
            order="created_at.desc",
            limit=10,
        )
    except Exception:
        history = []

    return analyses, history
