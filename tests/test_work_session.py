from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from workspace_telemetry.config import TelemetrySettings
from workspace_telemetry.work_sessions.catalog import normalize_path
from workspace_telemetry.work_sessions.config import WorkSessionSettings
from workspace_telemetry.work_sessions.service import WorkSessionService, _now
from workspace_telemetry.work_sessions.storage import atomic_json, read_json


class OfflineNotion:
    def data_source_id(self, value):
        return value

    def query_data_source(self, *_args, **_kwargs):
        raise RuntimeError("offline for test")

    def create_page(self, *_args, **_kwargs):
        raise RuntimeError("offline for test")

    def update_page(self, *_args, **_kwargs):
        raise RuntimeError("offline for test")


class OnlineNotion:
    def data_source_id(self, value):
        return value

    def query_data_source(self, *_args, **_kwargs):
        return []

    def create_page(self, *_args, **_kwargs):
        return {"id": "work-log-page"}

    def update_page(self, *_args, **_kwargs):
        return {"id": "work-log-page"}


class RecordingNotion:
    """Tracks create/update calls and can pre-seed an existing page so tests
    can assert a retried write updates it instead of creating a duplicate."""

    def __init__(self, existing_pages=None):
        self.pages = list(existing_pages or [])
        self.created = []
        self.updated = []

    def data_source_id(self, value):
        return value

    def query_data_source(self, _source_id, *, filter_payload=None, page_size=100):
        return list(self.pages)

    def create_page(self, _source_id, properties):
        page = {"id": f"work-log-page-{len(self.created) + 1}", "properties": properties}
        self.created.append(page)
        return page

    def update_page(self, page_id, properties):
        self.updated.append((page_id, properties))
        return {"id": page_id, "properties": properties}


class WorkSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        telemetry = TelemetrySettings(notion_api_key="test", projects_database_id="projects", work_log_database_id="worklogs")
        self.settings = WorkSessionSettings(
            telemetry=telemetry, state_directory=base, project_map_path=base / "project-map.json",
            project_cache_path=base / "projects-cache.json", session_path=base / "current-session.json",
            pending_path=base / "pending.json", telemetry_path=base / "events.jsonl", reminders_path=base / "reminders.json",
            terminal_journal_path=base / "terminal.jsonl", switch_minimum_seconds=90, switch_minimum_observations=3,
        )
        atomic_json(self.settings.project_cache_path, {
            "version": 1, "updatedAt": _now().isoformat(),
            "schema": {"title": {"type": "title", "writable": True}, "domain": {"type": "select", "writable": True, "options": ["Technology"]}, "status": {"type": "select", "writable": True, "options": ["Active"]}, "one liner": {"type": "rich_text", "writable": True}},
            "workLogSchema": {
                "entry": {"type": "title"}, "project": {"type": "relation"}, "date": {"type": "date"},
                "session start": {"type": "date"}, "session end": {"type": "date"}, "Session type": {"type": "multi_select", "options": ["Build", "Experiment", "Planning", "Research", "Learning", "Writing", "Organizing", "Reading"]}, "event_hash": {"type": "rich_text"}, "confidence": {"type": "number"},
            }, "workLogDataSourceId": "worklogs",
            "projects": [
                {"id": "alpha", "name": "Alpha", "notion": True, "url": "", "status": "Active", "domain": "Technology", "notionAssociations": {"paths": ["/work/alpha"], "repos": ["/work/alpha"], "workspaces": [], "domains": ["alpha.test"], "apps": [], "aliases": ["Alpha app"]}},
                {"id": "beta", "name": "Beta", "notion": True, "url": "", "status": "Active", "domain": "Technology", "notionAssociations": {"paths": [], "repos": ["/work/alpha/tools"], "workspaces": [], "domains": [], "apps": [], "aliases": []}},
            ],
        })
        atomic_json(self.settings.project_map_path, {"version": 1, "projects": {}})
        self.service = WorkSessionService(self.settings, notion=OfflineNotion())

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_path_normalization_and_child_directory_inheritance(self):
        self.assertEqual(normalize_path("/work/alpha/../alpha/src/main.py"), "/work/alpha/src/main.py")
        matches = self.service.match_context({"paths": ["/work/alpha/src/main.py"], "files": [], "workspaces": [], "repos": [], "domains": [], "application": "Code", "window": ""})
        self.assertEqual(matches[0].project_id, "alpha")
        self.assertGreater(matches[0].score, .9)

    def test_deepest_overlapping_path_wins_directory_inheritance(self):
        matches = self.service.match_context({"paths": ["/work/alpha/tools/run.py"], "files": [], "workspaces": [], "repos": [], "domains": [], "application": "Code", "window": ""})
        self.assertEqual(matches[0].project_id, "beta")
        self.assertTrue(self.service._unambiguous(matches, .82))

    def test_equal_strength_project_domain_matches_remain_uncertain(self):
        self.service.associate("beta", "domain", "shared.test")
        self.service.associate("alpha", "domain", "shared.test")
        matches = self.service.match_context({"paths": [], "files": [], "workspaces": [], "repos": [], "domains": ["shared.test"], "application": "Safari", "window": ""})
        self.assertFalse(self.service._unambiguous(matches, .5))

    def test_exact_local_file_association_beats_inherited_path(self):
        self.service.associate("alpha", "file", "/outside/special.blend")
        matches = self.service.match_context({"paths": [], "files": ["/outside/special.blend"], "workspaces": [], "repos": [], "domains": [], "application": "Blender", "window": ""})
        self.assertEqual(matches[0].project_id, "alpha")
        self.assertEqual(matches[0].signals[0]["kind"], "exact_file")

    def test_manual_association_survives_service_restart(self):
        self.service.associate("alpha", "path", "/new/project-root")
        restarted = WorkSessionService(self.settings, notion=OfflineNotion())
        result = restarted.observe({"application": "Finder", "window": "", "paths": ["/new/project-root/child/a.txt"]})
        self.assertEqual(result["project"]["projectId"], "alpha")

    def test_local_project_and_session_state_are_durable(self):
        local = self.service.create_project({"title": "Offline experiment", "status": "Active"}, create_in_notion=False)["project"]
        session = self.service.start_session({"projectIds": [local["id"]], "goal": "Try a local flow", "activityType": "Experimenting", "plannedMinutes": 15})["session"]
        self.assertEqual(read_json(self.settings.session_path, {})["session_id"], session["session_id"])
        self.assertEqual(WorkSessionService(self.settings, notion=OfflineNotion()).current_session()["status"], "active")

    def test_archived_notion_projects_do_not_reappear_as_local_drafts(self):
        mapping = read_json(self.settings.project_map_path, {})
        mapping["projects"]["archived-project"] = {
            "name": "Archived remote", "notion_page_id": "archived-project", "paths": ["/old/project"],
        }
        atomic_json(self.settings.project_map_path, mapping)
        restarted = WorkSessionService(self.settings, notion=OfflineNotion())
        self.assertNotIn("archived-project", {project["id"] for project in restarted.catalog.combined_projects()})

    def test_selected_reminders_persist_for_checkpoint_completion(self):
        session = self.service.start_session({
            "projectIds": ["alpha"], "goal": "Use a reminder", "activityType": "Planning",
            "reminderIds": ["reminder-1"],
            "selectedReminders": [{"id": "reminder-1", "name": "Plan Alpha", "list": "Tasks", "body": "never persist this"}],
        })["session"]
        self.assertEqual(session["selected_reminders"], [{"id": "reminder-1", "name": "Plan Alpha", "list": "Tasks", "dueDate": ""}])
        finished = self.service.finish_session({"outcome": "Completed", "completeReminderIds": ["reminder-1"]})["session"]
        self.assertEqual(finished["completed_reminder_ids"], ["reminder-1"])

    def test_multiple_project_session_and_switch_threshold(self):
        session = self.service.start_session({"projectIds": ["alpha", "beta"], "primaryProjectId": "alpha", "goal": "Bridge projects", "activityType": "Building / Implementing"})["session"]
        changed = self.service.change_projects(["alpha", "beta"], primary_project_id="beta")
        self.assertEqual(changed["session"]["primary_project_id"], "beta")
        session = self.service.current_session()
        session["project_ids"] = ["alpha"]
        session["switch_candidate"] = {"project_id": "beta", "observations": 2, "confidence": .95, "first_seen_at": (_now() - timedelta(seconds=100)).isoformat()}
        self.service._save_session(session)
        result = self.service.observe({"application": "Code", "window": "", "paths": ["/work/alpha/tools/x.py"]})
        self.assertEqual(result["projectSwitchSuggested"]["detected"]["projectId"], "beta")

    def test_reminder_ranking_prefers_project_match_and_overdue_priority(self):
        atomic_json(self.settings.reminders_path, {"reminders": [
            {"id": "generic", "name": "Later", "completed": False, "priority": 0, "dueDate": ""},
            {"id": "project", "name": "Finish Alpha app", "completed": False, "priority": 0, "dueDate": ""},
            {"id": "urgent", "name": "Other", "completed": False, "priority": 1, "dueDate": "2020-01-01T00:00:00+00:00"},
        ]})
        ids = [item["id"] for item in self.service.rank_reminders(["alpha"])]
        self.assertEqual(ids[:2], ["urgent", "project"])

    def test_offline_work_log_is_queued_not_lost(self):
        self.service.start_session({"projectIds": ["alpha"], "goal": "Queue safely", "activityType": "Planning"})
        result = self.service.finish_session({"outcome": "Made progress", "accomplishment": "Kept the local record."})
        self.assertGreaterEqual(result["sync"]["remaining"], 1)
        pending = read_json(self.settings.pending_path, {})
        self.assertEqual(pending["operations"][0]["kind"], "work_log")

    def test_immediately_synced_work_log_is_returned_in_session_state(self):
        self.service = WorkSessionService(self.settings, notion=OnlineNotion())
        self.service.start_session({"projectIds": ["alpha"], "goal": "Open the result", "activityType": "Planning"})
        result = self.service.finish_session({"outcome": "Completed"})
        self.assertEqual(result["session"]["notion_work_log_id"], "work-log-page")

    def test_retried_work_log_write_updates_existing_page_instead_of_duplicating(self):
        # Regression test: a prior attempt for this exact session may already
        # have created the Notion page before failing later (e.g. a crash
        # before the pending-operation queue persisted its removal). Replaying
        # `_write_work_log` for the same session must find that page by its
        # event_hash and update it rather than creating a second Work Log row.
        notion = RecordingNotion()
        self.service = WorkSessionService(self.settings, notion=notion)
        self.service.start_session({"projectIds": ["alpha"], "goal": "Retry safely", "activityType": "Planning"})
        session = self.service.current_session()
        first = self.service._write_work_log(session)
        self.assertEqual(len(notion.created), 1)
        self.assertEqual(len(notion.updated), 0)
        event_hash = f"work-session:{session['session_id']}"
        notion.pages = [{
            "id": first["pageId"],
            "properties": {"event_hash": {"type": "rich_text", "rich_text": [{"plain_text": event_hash}]}},
        }]
        second = self.service._write_work_log(session)
        self.assertEqual(second["pageId"], first["pageId"])
        self.assertEqual(len(notion.created), 1, "a retry must not create a second Notion page")
        self.assertEqual(len(notion.updated), 1)

    def test_incorrect_associations_can_be_removed(self):
        self.service.associate("alpha", "domain", "example.test")
        self.assertTrue(self.service.disassociate("alpha", "domain", "example.test")["removed"])
        self.assertFalse(self.service.match_context({"paths": [], "files": [], "workspaces": [], "repos": [], "domains": ["example.test"], "application": "Safari", "window": ""}))

    def test_blender_suggestion_is_visible_but_not_silently_persisted(self):
        suggestion = self.service.classification_suggestion({"projectId": "alpha", "application": "Blender"})
        self.assertEqual(suggestion["workModes"], ["Creative", "Design"])
        self.assertEqual(suggestion["activityMethod"], "3D Modeling")
        self.assertGreaterEqual(suggestion["confidence"], .9)
        self.assertFalse((self.settings.state_directory / "classification-history.json").exists())

    def test_completed_classification_is_learned_and_written_to_existing_session_type(self):
        self.service.start_session({
            "projectIds": ["alpha"], "activityType": "Creative Work",
            "workModes": ["Creative", "Engineering"], "activityMethod": "3D Modeling",
            "classificationConfidence": .91, "classificationProvenance": "suggested_confirmed",
            "projectConfidence": .96, "projectProvenance": "path_prefix",
        })
        completed = self.service.finish_session({"outcome": "Completed"})["session"]
        self.assertTrue((self.settings.state_directory / "classification-history.json").exists())
        props = self.service._work_log_properties(self.service.catalog.cache()["workLogSchema"], completed, ["alpha"])
        self.assertEqual(props["Session type"]["multi_select"], [{"name": "Experiment"}, {"name": "Build"}])
        self.assertEqual(props["confidence"]["number"], .91)


if __name__ == "__main__":
    unittest.main()
