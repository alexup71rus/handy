"""Personal fact contracts, independent of folders and neural parsers."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from folder_memory.personal_memory import MemoryConflictError, PersonalMemory


class PersonalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.memory = PersonalMemory(self.db)

    def prepare(self, value="8 800 123-45-67", subject="Сервис-X", field="телефон поддержки"):
        return self.memory.prepare(subject, field, value, "Запомни телефон поддержки Сервис-X: " + value)

    def save(self, value="8 800 123-45-67", op="question-one"):
        result = self.memory.save(self.prepare(value), op)
        self.db.commit()
        return result

    def test_prepare_is_a_read_only_preview(self):
        before = self.db.total_changes
        proposal = self.prepare()
        self.assertEqual(self.db.total_changes, before)
        self.assertIsNone(proposal["prior"])
        self.assertEqual(proposal["scope"], "personal")
        self.assertEqual(self.memory.recall("Сервис-X"), [])

    def test_save_leaves_commit_to_the_core(self):
        self.memory.save(self.prepare(), "question-one")
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(len(self.memory.recall("Сервис-X")), 1)
        self.db.rollback()
        self.assertEqual(self.memory.recall("Сервис-X"), [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM personal_memory_operations").fetchone()[0], 0)

    def test_personal_fact_is_available_without_any_workspace(self):
        record = self.save()
        other_component = PersonalMemory(self.db)
        recalled = other_component.recall("сервис-x", "телефон поддержки")
        self.assertEqual(recalled[0]["id"], record["id"])
        self.assertEqual(recalled[0]["value"], "8 800 123-45-67")
        self.assertEqual(recalled[0]["source"], "user-confirmed")
        self.assertEqual(recalled[0]["scope"], "personal")

    def test_key_normalization_preserves_the_actual_value(self):
        proposal = self.memory.prepare("  Ёлка   Сервис ", " Номер  ПОДДЕРЖКИ ", "AbC42", "Пользователь указал AbC42")
        self.memory.save(proposal, "case")
        self.db.commit()
        found = self.memory.recall("елка сервис", "номер поддержки")
        self.assertEqual(found[0]["value"], "AbC42")

    def test_new_value_previews_the_previous_assertion(self):
        original = self.save()
        proposal = self.prepare("8 800 999-00-11")
        self.assertEqual(proposal["prior"]["id"], original["id"])
        self.assertEqual(proposal["prior"]["value"], original["value"])
        self.assertEqual(proposal["expected_version"], 1)
        self.assertTrue(proposal["changes_value"])
        self.assertEqual(self.memory.recall("Сервис-X")[0]["value"], original["value"])

    def test_updates_keep_history_and_one_active_record(self):
        original = self.save()
        updated = self.save("8 800 999-00-11", "question-two")
        self.assertEqual(original["id"], updated["id"])
        self.assertEqual(updated["version"], 2)
        self.assertEqual(len(self.memory.recall("Сервис-X")), 1)
        self.assertEqual([r["value"] for r in self.memory.history(original["id"])], [original["value"], updated["value"]])

    def test_identical_value_does_not_duplicate_or_revise_a_fact(self):
        original = self.save()
        same = self.save(op="question-two")
        self.assertEqual(original, same)
        self.assertEqual(len(self.memory.history(original["id"])), 1)

    def test_replay_returns_its_original_result_after_later_updates(self):
        proposal = self.prepare()
        first = self.memory.save(proposal, "question-one");self.db.commit()
        self.save("8 800 999-00-11", "question-two")
        replay = self.memory.save(proposal, "question-one")
        self.assertEqual(first, replay)
        self.assertEqual(self.memory.recall("Сервис-X")[0]["version"], 2)

    def test_operation_id_cannot_be_reused_for_another_value(self):
        self.save()
        with self.assertRaises(ValueError):
            self.memory.save(self.prepare("different"), "question-one")
        self.assertEqual(self.memory.recall("Сервис-X")[0]["version"], 1)

    def test_stale_confirmation_cannot_overwrite_a_concurrent_change(self):
        self.save()
        stale = self.prepare("stale value")
        self.save("newer value", "question-two")
        with self.assertRaises(MemoryConflictError):
            self.memory.save(stale, "stale-question")
        self.assertEqual(self.memory.recall("Сервис-X")[0]["value"], "newer value")
        self.assertEqual(len(self.memory.history(1)), 2)

    def test_failed_later_step_can_roll_back_fact_and_operation_together(self):
        self.db.execute("CREATE TABLE pending_test(id TEXT,status TEXT)")
        self.db.execute("INSERT INTO pending_test VALUES('question-one','pending')")
        self.db.commit()
        try:
            self.db.execute("BEGIN")
            self.memory.save(self.prepare(), "question-one")
            self.db.execute("UPDATE pending_test SET status='resolved'")
            raise RuntimeError("failure after memory write")
        except RuntimeError:
            self.db.rollback()
        self.assertEqual(self.memory.recall("Сервис-X"), [])
        self.assertEqual(self.db.execute("SELECT status FROM pending_test").fetchone()[0], "pending")

    def test_grounding_rejects_a_hallucinated_or_reformatted_value(self):
        for value, evidence in [("8 800 999-00-11", "Номер 8 800 123-45-67"), ("abc", "Код AbC"), ("+79990000000", "Номер 8 (999) 000-00-00")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.memory.prepare("Сервис", "контакт", value, evidence)
        self.assertEqual(self.memory.recall("Сервис"), [])

    def test_grounding_accepts_whitespace_equivalence(self):
        proposal = self.memory.prepare("Сервис", "номер", "8 800 123", "Номер: 8\n800\t123")
        self.memory.save(proposal, "whitespace")
        self.assertEqual(self.memory.recall("Сервис")[0]["value"], "8 800 123")

    def test_unknown_subject_or_field_is_not_filled_from_another_fact(self):
        self.save()
        self.assertEqual(self.memory.recall("другой сервис"), [])
        self.assertEqual(self.memory.recall("Сервис-X", "почтовый адрес"), [])
        self.assertEqual(self.memory.recall("Сервис"), [])

    def test_punctuation_and_field_inflection_match_conservatively(self):
        self.save()
        self.assertEqual(self.memory.recall("Сервис X", "телефон поддержки")[0]["value"], "8 800 123-45-67")
        self.assertEqual(self.memory.recall("Сервис-X", "поддержка")[0]["value"], "8 800 123-45-67")

    def add_subjects(self, names):
        self.db.executemany(
            "INSERT INTO personal_facts(subject,subject_key,field,field_key,value,evidence,source,version,created,updated) VALUES(?,?,'contact','contact','fixture','fixture','user-confirmed',1,0,0)",
            [(name, name.casefold()) for name in names],
        )
        self.db.commit()

    def test_unrelated_subjects_do_not_displace_a_punctuation_match(self):
        self.add_subjects("a-unrelated-" + str(number) for number in range(2001))
        proposal = self.prepare(subject="Target-Service")
        self.memory.save(proposal, "target"); self.db.commit()
        recalled = self.memory.recall("Target Service", "телефон поддержки")
        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0]["subject"], "Target-Service")

    def test_punctuation_ambiguity_beyond_the_old_global_budget_is_not_hidden(self):
        first = self.prepare(subject="Service-X")
        self.memory.save(first, "first"); self.db.commit()
        self.add_subjects("servicea" + str(number) for number in range(2001))
        second = self.prepare("8 800 999-00-11", subject="Service~X")
        self.memory.save(second, "second"); self.db.commit()
        self.assertEqual(self.memory.recall("Service.X"), [])
        self.assertEqual(self.memory.recall("Service-X")[0]["value"], first["value"])

    def test_incomplete_candidate_search_does_not_prove_unique_identity(self):
        self.add_subjects("service" + str(number) + "x" for number in range(2001))
        self.memory.save(self.prepare(subject="Service-X"), "exact"); self.db.commit()
        self.assertEqual(self.memory.recall("Service X"), [])
        self.assertEqual(len(self.memory.recall("Service-X")), 1)

    def test_invalid_lengths_controls_and_limits_are_rejected(self):
        for args in [("", "field", "value", "value"), ("x" * 241, "field", "value", "value"), ("subject", "f" * 161, "value", "value"), ("subject", "field", "x" * 4001, "x" * 4001), ("subject", "field", "a\0b", "a\0b"), ("subject", "field", "value", "x" * 16001)]:
            with self.subTest(lengths=tuple(len(v) for v in args)), self.assertRaises(ValueError):
                self.memory.prepare(*args)
        for limit in [0, 21, True, "5"]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.memory.recall("subject", limit=limit)

    def test_persistence_across_connections(self):
        from contextlib import closing
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                memory = PersonalMemory(db)
                memory.save(memory.prepare("Сервис", "номер", "123", "Номер 123"), "operation")
            with closing(sqlite3.connect(path)) as db, db:
                memory = PersonalMemory(db)
                self.assertEqual(memory.recall("Сервис")[0]["value"], "123")


if __name__ == "__main__":
    unittest.main()
