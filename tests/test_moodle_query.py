import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "moodle_query.py"


def load_module():
    spec = importlib.util.spec_from_file_location("moodle_query", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MoodleQueryFileTextTests(unittest.TestCase):
    def test_moodle_pluginfile_url_gets_token_without_forcedownload(self):
        module = load_module()

        result = module.tokenized_file_url(
            "https://moodle.example/webservice/pluginfile.php/1/mod_assign/intro/file.pdf",
            "abc123",
        )

        self.assertEqual(
            result,
            "https://moodle.example/webservice/pluginfile.php/1/mod_assign/intro/file.pdf?token=abc123",
        )

    def test_collect_assignment_files_includes_intro_and_submitted_files(self):
        module = load_module()
        assignment = {
            "introattachments": [
                {
                    "filename": "project.pdf",
                    "fileurl": "https://moodle.example/webservice/pluginfile.php/1/mod_assign/intro/project.pdf",
                    "mimetype": "application/pdf",
                }
            ]
        }
        submission_status = {
            "lastattempt": {
                "submission": {
                    "plugins": [
                        {
                            "type": "file",
                            "fileareas": [
                                {
                                    "files": [
                                        {
                                            "filename": "HW1_1_2.pdf",
                                            "fileurl": "https://moodle.example/webservice/pluginfile.php/2/submission/HW1_1_2.pdf",
                                            "mimetype": "application/pdf",
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }
            }
        }

        files = module.collect_assignment_files(assignment, submission_status)

        self.assertEqual([file["filename"] for file in files], ["project.pdf", "HW1_1_2.pdf"])

    def test_filter_files_uses_case_insensitive_filename_substring(self):
        module = load_module()
        files = [
            {"filename": "ProjectEx2.pdf", "fileurl": "https://example/ProjectEx2.pdf"},
            {"filename": "HW1.pdf", "fileurl": "https://example/HW1.pdf"},
        ]

        self.assertEqual(module.filter_files(files, "project"), [files[0]])


if __name__ == "__main__":
    unittest.main()
