from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from sourcebridge.cli import main
from sourcebridge.pipeline import initialize
from sourcebridge.rule_selection import select_next_rule


class RuleSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = self.root / "evidence.db"
        initialize(self.database)
        self.connection = sqlite3.connect(self.database)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            INSERT INTO sourcebridge_profile VALUES
              ('profile-1','demo','frontend','corpus','backend','rules','adapters','options','now');
            INSERT INTO sourcebridge_corpus VALUES
              ('corpus','demo','1',NULL,'digest','now');
            INSERT INTO sourcebridge_variant VALUES
              ('variant','corpus','frontend','compiler','abi',64,'little','config','input','now');
            INSERT INTO sourcebridge_source_unit VALUES
              ('unit-1','variant','UnitOne','unit.src','1:1','source','source');
            INSERT INTO sourcebridge_component_decision VALUES
              ('decision-1','profile-1','unit-1','direct_translation','backend',NULL,NULL,
               'covered','test','now');
            INSERT INTO sourcebridge_work_item VALUES
              ('work-1','decision-1','diagnostic','open',NULL,'wide blocker','now','now');
            INSERT INTO sourcebridge_execution_run
              (id, profile_id, variant_id, action, requested_workers, status,
               command_json, requested_by, started_at, completed_at, created_at)
              VALUES ('run-1','profile-1','variant','validate-target',1,'passed',
                      '{}','test','now','now','now');
            INSERT INTO sourcebridge_validation_result VALUES
              ('validation-1','decision-1','target','validator','failed','target.log','evidence',
               '{}','now');
            INSERT INTO sourcebridge_diagnostic_occurrence VALUES
              ('diag-1','validation-1','unit-1',NULL,NULL,NULL,'target','validator','E1',
               'error','signature','failure','{}','now');
            INSERT INTO sourcebridge_work_item_evidence VALUES
              ('link-1','work-1','run-1','diag-1','validation-1',NULL,NULL,'diagnostic','now');
            """
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_blocker_and_completion_are_read_only_candidates(self) -> None:
        blocker = select_next_rule(self.database, "blocker/coverage")
        self.assertEqual(blocker["candidate"], "work-1")
        self.assertEqual(blocker["evidence_references"], ["diag-1"])
        completion = select_next_rule(self.database, "completion", profile_id="profile-1")
        self.assertEqual(completion["candidate"], "unit-1")
        self.assertEqual(completion["gap"], "validation")

    def test_basics_reads_first_ordered_curriculum_entry(self) -> None:
        curriculum = self.root / "curriculum.json"
        curriculum.write_text(json.dumps({"entries": [
            {"id": "basic-1", "evidence_references": ["doc-1"],
             "test_references": ["test-1"], "expected_profile_scope": {"source": "demo"}}
        ]}))
        manifest = self.root / "pack.json"
        manifest.write_text(json.dumps(
            {"entrypoints": {"rule_curriculum": "curriculum.json"}}
        ))
        result = select_next_rule(self.database, "basics", pack_manifest=manifest)
        self.assertEqual(result["candidate"], "basic-1")
        self.assertEqual(result["test_references"], ["test-1"])

    def test_cli_emits_one_json_candidate(self) -> None:
        import contextlib
        import io
        output = io.StringIO()
        original = sys.argv
        sys.argv = ["sourcebridge", "select-next-rule", "blocker/coverage",
                    "--database", str(self.database)]
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        sys.argv = original
        self.assertEqual(json.loads(output.getvalue())["candidate"], "work-1")
