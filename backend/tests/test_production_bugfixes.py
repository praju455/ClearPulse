import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from routes import analyze, records, triage
from services import insforge


class TriageTests(unittest.IsolatedAsyncioTestCase):
    def test_history_drops_ui_greeting_and_merges_duplicate_roles(self):
        history = triage._gemini_history([
            triage.TriageMessage(role="assistant", content="Welcome"),
            triage.TriageMessage(role="user", content="Fever"),
            triage.TriageMessage(role="user", content="Since today"),
            triage.TriageMessage(role="assistant", content="Any other symptoms?"),
        ])

        self.assertEqual([message.role for message in history], ["user", "model"])
        self.assertEqual(history[0].parts[0].text, "Fever\nSince today")

    async def test_chat_returns_safe_response_when_all_ai_providers_fail(self):
        request = triage.TriageRequest(
            patient_id="patient-test",
            message="I have fever",
            history=[triage.TriageMessage(role="assistant", content="Welcome")],
        )
        with (
            patch.object(triage, "get_gemini_client", side_effect=RuntimeError("no key")),
            patch.object(triage, "get_groq_client", return_value=None),
            patch.object(triage, "db_insert", new=AsyncMock()),
        ):
            result = await triage.triage_chat(request)

        self.assertEqual(result["triage_level"], "Assessing")
        self.assertEqual(result["ai_status"], "safe_fallback")
        self.assertIn("temperature", result["response"].lower())


class AccessGrantTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_compound_update_revokes_matching_grant_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_path = insforge.LOCAL_DB_PATH
            original_base_url = insforge.INSFORGE_BASE_URL
            original_key = insforge.INSFORGE_SERVICE_KEY
            try:
                insforge.LOCAL_DB_PATH = Path(tmp) / "db.json"
                insforge.INSFORGE_BASE_URL = ""
                insforge.INSFORGE_SERVICE_KEY = ""
                await insforge.db_insert("access_grants", {
                    "analysis_id": "analysis-1",
                    "doctor_wallet": "doctor-1",
                    "is_active": True,
                })
                await insforge.db_insert("access_grants", {
                    "analysis_id": "analysis-1",
                    "doctor_wallet": "doctor-2",
                    "is_active": True,
                })

                updated = await insforge.db_update_where(
                    "access_grants",
                    {"analysis_id": "analysis-1", "doctor_wallet": "doctor-1", "is_active": "true"},
                    {"is_active": False},
                )
                rows = await insforge.db_select("access_grants", {"analysis_id": "analysis-1"})
            finally:
                insforge.LOCAL_DB_PATH = original_path
                insforge.INSFORGE_BASE_URL = original_base_url
                insforge.INSFORGE_SERVICE_KEY = original_key

        self.assertEqual(len(updated), 1)
        states = {row["doctor_wallet"]: row["is_active"] for row in rows}
        self.assertFalse(states["doctor-1"])
        self.assertTrue(states["doctor-2"])

    async def test_revoke_endpoint_uses_filtered_database_update(self):
        updater = AsyncMock(return_value=[{"id": "grant-1", "is_active": False}])
        with patch.object(records, "db_update_where", new=updater):
            result = await records.revoke_access(records.RevokeAccessRequest(
                doctor_wallet="doctor-1",
                analysis_id="analysis-1",
            ))

        self.assertEqual(result, {"success": True, "revoked_count": 1})
        updater.assert_awaited_once()


class ReportAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failure_does_not_save_fake_analysis(self):
        request = analyze.AnalyzeRequest(
            file_base64=base64.b64encode(b"redacted medical report").decode(),
            file_type="text/plain",
            patient_wallet="patient-test",
            file_name="report.txt",
        )
        inserter = AsyncMock()
        with (
            patch.object(analyze, "get_gemini_client", side_effect=RuntimeError("no key")),
            patch.object(analyze, "get_groq_client", return_value=None),
            patch.object(analyze, "db_insert", new=inserter),
        ):
            with self.assertRaises(HTTPException) as raised:
                await analyze.analyze_report(request)

        self.assertEqual(raised.exception.status_code, 503)
        inserter.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
