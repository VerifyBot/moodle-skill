import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import moodle_cli


class MoodleCliTests(unittest.TestCase):
    def test_load_config_uses_env_token_before_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.ini"
            config_path.write_text(
                "[moodle]\n"
                "web_service_token=config-token\n"
                "domain=moodle25.technion.ac.il\n",
                encoding="utf-8",
            )
            old_env = os.environ.get("MOODLE_WSTOKEN")
            os.environ["MOODLE_WSTOKEN"] = "env-token"
            try:
                config = moodle_cli.load_config(config_path)
            finally:
                if old_env is None:
                    os.environ.pop("MOODLE_WSTOKEN", None)
                else:
                    os.environ["MOODLE_WSTOKEN"] = old_env

        self.assertEqual(config.token, "env-token")
        self.assertEqual(
            config.base_url,
            "https://moodle25.technion.ac.il/webservice/rest/server.php",
        )

    def test_load_config_extracts_webservice_token_from_encoded_triple_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.ini"
            config_path.write_text(
                "[moodle]\n"
                "web_service_token=wrong-token\n"
                "token=YWxwaGE6OjozMmNoYXJ3ZWJzZXJ2aWNldG9rZW5oZXJlOjo6b21pdHRlZA==\n"
                "domain=moodle25.technion.ac.il\n",
                encoding="utf-8",
            )

            config = moodle_cli.load_config(config_path)

        self.assertEqual(config.token, "32charwebservicetokenhere")

    def test_request_raises_for_moodle_error_payload(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "exception": "core\\exception\\moodle_exception",
            "errorcode": "invalidtoken",
            "message": "Invalid token",
        }
        session = Mock()
        session.post.return_value = response
        client = moodle_cli.MoodleClient(
            base_url="https://example.test/webservice/rest/server.php",
            token="bad-token",
            session=session,
        )

        with self.assertRaisesRegex(moodle_cli.MoodleAPIError, "invalidtoken"):
            client.request("core_webservice_get_site_info")

    def test_submission_status_enriches_due_date_from_assignments(self):
        class FakeClient:
            def request(self, wsfunction, params=None):
                if wsfunction == "mod_assign_get_submission_status":
                    return {"lastattempt": {"submission": {"status": "new"}, "gradingstatus": "notgraded"}}
                if wsfunction == "mod_assign_get_assignments":
                    return {
                        "courses": [
                            {
                                "assignments": [
                                    {"id": 42, "name": "HW1", "duedate": 1900000000},
                                ]
                            }
                        ]
                    }
                raise AssertionError(wsfunction)

        output = moodle_cli.cmd_submission_status(
            Mock(assignid=42),
            FakeClient(),
        )

        self.assertIn("Due Date:", output)
        self.assertIn("HW1", output)

    def test_filter_hidden_courses_omits_hidden_by_default(self):
        courses = [
            {"id": 1, "fullname": "Visible", "hidden": False},
            {"id": 2, "fullname": "Hidden", "hidden": True},
        ]

        self.assertEqual(moodle_cli.filter_hidden_courses(courses), [courses[0]])
        self.assertEqual(moodle_cli.filter_hidden_courses(courses, include_hidden=True), courses)

    def test_cmd_courses_hides_hidden_courses_by_default(self):
        class FakeClient:
            def request(self, wsfunction, params=None):
                if wsfunction == "core_webservice_get_site_info":
                    return {"userid": 7}
                if wsfunction == "core_enrol_get_users_courses":
                    return [
                        {"id": 1, "shortname": "VISIBLE", "fullname": "Visible", "hidden": False},
                        {"id": 2, "shortname": "HIDDEN", "fullname": "Hidden", "hidden": True},
                    ]
                raise AssertionError(wsfunction)

        output = moodle_cli.cmd_courses(Mock(userid=None, include_hidden=False), FakeClient())

        self.assertIn("VISIBLE", output)
        self.assertNotIn("HIDDEN", output)

    def test_cmd_assignments_uses_only_visible_courses_by_default(self):
        calls = []

        class FakeClient:
            def request(self, wsfunction, params=None):
                calls.append((wsfunction, params))
                if wsfunction == "core_webservice_get_site_info":
                    return {"userid": 7}
                if wsfunction == "core_enrol_get_users_courses":
                    return [
                        {"id": 1, "fullname": "Visible", "hidden": False},
                        {"id": 2, "fullname": "Hidden", "hidden": True},
                    ]
                if wsfunction == "mod_assign_get_assignments":
                    return {"courses": [{"fullname": "Visible", "assignments": []}]}
                raise AssertionError(wsfunction)

        moodle_cli.cmd_assignments(Mock(courseid=None, userid=None, include_hidden=False), FakeClient())

        self.assertIn(("mod_assign_get_assignments", [("courseids[0]", 1)]), calls)


    def test_append_token_preserves_existing_query_string(self):
        self.assertEqual(
            moodle_cli.append_token_to_file_url(
                "https://example.test/webservice/pluginfile.php/1/file.pdf?forcedownload=1",
                "abc123",
            ),
            "https://example.test/webservice/pluginfile.php/1/file.pdf?forcedownload=1&token=abc123",
        )
        self.assertEqual(
            moodle_cli.append_token_to_file_url(
                "https://example.test/pluginfile.php/1/file.pdf",
                "abc123",
            ),
            "https://example.test/pluginfile.php/1/file.pdf?token=abc123",
        )

    def test_append_token_does_not_touch_external_urls(self):
        self.assertEqual(
            moodle_cli.append_token_to_file_url(
                "https://colab.research.google.com/drive/example?usp=sharing",
                "abc123",
            ),
            "https://colab.research.google.com/drive/example?usp=sharing",
        )

    def test_clean_text_removes_html_and_unescapes_entities(self):
        self.assertEqual(
            moodle_cli.clean_text("<p>Hello&nbsp;<strong>world</strong><br>again</p>"),
            "Hello world again",
        )

    def test_format_courses_outputs_markdown_table(self):
        markdown = moodle_cli.format_courses(
            [
                {"id": 11, "shortname": "CS101", "fullname": "Intro <b>CS</b>"},
            ]
        )

        self.assertIn("| Course ID | Short Name | Full Name |", markdown)
        self.assertIn("| 11 | CS101 | Intro CS |", markdown)


if __name__ == "__main__":
    unittest.main()
