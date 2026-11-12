"""Provider checkbox transitions do not change model selection or execution."""

from copy import deepcopy
import unittest

from folder_memory.model_registry import ModelRegistry
from folder_memory.model_visibility import (
    STANDARD_PROVIDER, provider_visibility_state, set_model_visibility, set_provider_visibility,
)


class ModelVisibilityTests(unittest.TestCase):
    def settings(self, count=100):
        profiles = {f"model-{index}": {"kind": "chat", "backend": "openai", "enabled": True,
                    "name": f"Model {index}", "provider_id": "api", "model_id": f"model-{index}",
                    "base_url": "https://provider.example/v1", "context": 65536}
                    for index in range(count)}
        profiles.update({
            "api-encoder": {"kind": "embedding", "backend": "openai", "enabled": True,
                            "name": "API encoder", "provider_id": "api", "model_id": "embed",
                            "base_url": "https://provider.example/v1", "dimensions": 3, "context": 512},
            "local": {"kind": "chat", "backend": "local", "enabled": True, "name": "Local",
                      "model": "/models/local.gguf", "binary": "/runtime/llama-server", "context": 65536},
            "other": {"kind": "chat", "backend": "openai", "enabled": True, "name": "Other",
                      "provider_id": "other", "base_url": "https://other.example/v1", "model_id": "other", "context": 65536},
            "flat": {"kind": "chat", "backend": "openai", "enabled": True, "name": "Legacy",
                     "base_url": "https://flat.example/v1", "model_id": "legacy", "context": 65536},
        })
        return {"version": 1, "profiles": profiles,
                "providers": {"api": {"name": "API", "base_url": "https://provider.example/v1"},
                              "other": {"name": "Other", "base_url": "https://other.example/v1"}},
                "selected_chat": "model-0" if count else "local", "selected_embedding": "api-encoder"}

    def test_one_parent_click_hides_all_hundred_models_and_encoder(self):
        data = self.settings()
        before_other = deepcopy(data["profiles"]["other"])
        self.assertEqual(provider_visibility_state(data, "api"), "on")
        self.assertIs(set_provider_visibility(data, "api", False), data)
        self.assertEqual(provider_visibility_state(data, "api"), "off")
        self.assertFalse(data["providers"]["api"]["visible"])
        self.assertTrue(all(profile["visible"] is False for profile in data["profiles"].values() if profile.get("provider_id") == "api"))
        self.assertEqual(data["profiles"]["other"], before_other)

    def test_off_then_one_child_becomes_mixed_and_leaves_siblings_off(self):
        data = self.settings()
        set_provider_visibility(data, "api", False)
        set_model_visibility(data, "model-21", True)
        self.assertTrue(data["providers"]["api"]["visible"])
        self.assertTrue(data["profiles"]["model-21"]["visible"])
        self.assertEqual(provider_visibility_state(data, "api"), "mixed")
        self.assertEqual([key for key, value in data["profiles"].items() if value.get("provider_id") == "api" and value.get("visible", True)], ["model-21"])

    def test_legacy_disabled_parent_clears_old_true_flags_before_enabling_one(self):
        data = self.settings()
        data["providers"]["api"]["visible"] = False
        self.assertNotIn("visible", data["profiles"]["model-0"])
        set_model_visibility(data, "model-7", True)
        self.assertEqual(provider_visibility_state(data, "api"), "mixed")
        self.assertEqual(sum(value.get("visible", True) for value in data["profiles"].values() if value.get("provider_id") == "api"), 1)
        self.assertTrue(data["profiles"]["other"].get("visible", True))

    def test_mixed_parent_check_enables_all_children(self):
        data = self.settings()
        set_model_visibility(data, "model-3", False)
        self.assertEqual(provider_visibility_state(data, "api"), "mixed")
        set_provider_visibility(data, "api", True)
        self.assertEqual(provider_visibility_state(data, "api"), "on")
        self.assertTrue(all(value["visible"] for value in data["profiles"].values() if value.get("provider_id") == "api"))

    def test_disabling_one_child_keeps_parent_and_other_models_available(self):
        data = self.settings()
        set_model_visibility(data, "model-0", False)
        self.assertNotIn("visible", data["providers"]["api"])
        self.assertTrue(data["profiles"]["model-1"].get("visible", True))
        self.assertEqual(provider_visibility_state(data, "api", kind="chat"), "mixed")
        self.assertEqual(provider_visibility_state(data, "api", kind="embedding"), "on")

    def test_no_children_and_all_disabled_children_report_off(self):
        data = self.settings()
        data["providers"]["empty"] = {"name": "Empty", "base_url": "https://empty.example/v1"}
        self.assertEqual(provider_visibility_state(data, "empty"), "off")
        for key, profile in data["profiles"].items():
            if profile.get("provider_id") == "api":
                set_model_visibility(data, key, False)
        self.assertEqual(provider_visibility_state(data, "api"), "off")

    def test_standard_group_restores_missing_catalogue_without_overwriting_custom_values(self):
        data = self.settings()
        data["profiles"]["local"]["context"] = 8192
        catalogue = {"local": {"kind": "chat", "backend": "local", "context": 65536},
                     "missing": {"kind": "chat", "backend": "local", "name": "Missing", "model": "/missing.gguf"}}
        previous = deepcopy(catalogue)
        set_provider_visibility(data, STANDARD_PROVIDER, False, catalogue=catalogue)
        self.assertIn("missing", data["profiles"])
        self.assertEqual(data["profiles"]["local"]["context"], 8192)
        self.assertEqual(provider_visibility_state(data, STANDARD_PROVIDER), "off")
        set_model_visibility(data, "missing", True)
        self.assertTrue(data["standard_provider_visible"])
        self.assertFalse(data["profiles"]["local"]["visible"])
        self.assertEqual(provider_visibility_state(data, STANDARD_PROVIDER), "mixed")
        self.assertEqual(catalogue, previous)

    def test_legacy_standard_mask_does_not_revive_other_local_models(self):
        data = self.settings()
        data["profiles"]["local-2"] = {**data["profiles"]["local"], "name": "Second local"}
        data["standard_provider_visible"] = False
        set_model_visibility(data, "local-2", True)
        self.assertEqual(provider_visibility_state(data, STANDARD_PROVIDER), "mixed")
        self.assertTrue(data["profiles"]["local-2"]["visible"])
        self.assertFalse(data["profiles"]["local"]["visible"])

    def test_flat_api_model_is_its_own_owner(self):
        data = self.settings()
        before = deepcopy(data)
        set_provider_visibility(data, "legacy:flat", False)
        self.assertEqual(provider_visibility_state(data, "legacy:flat"), "off")
        set_model_visibility(data, "flat", True)
        self.assertEqual(provider_visibility_state(data, "legacy:flat"), "on")
        for key in data["profiles"]:
            if key != "flat":
                self.assertEqual(data["profiles"][key], before["profiles"][key])
        self.assertEqual(data["providers"], before["providers"])

    def test_runtime_configuration_and_selected_models_do_not_change(self):
        data = self.settings()
        expected = ModelRegistry._resolved(data)
        selection = (data["selected_chat"], data["selected_embedding"])
        set_provider_visibility(data, "api", False)
        set_model_visibility(data, "model-5", True)
        set_provider_visibility(data, STANDARD_PROVIDER, False)
        self.assertEqual(ModelRegistry._resolved(data), expected)
        self.assertEqual((data["selected_chat"], data["selected_embedding"]), selection)

    def test_unknown_ids_and_invalid_visibility_never_mutate_document(self):
        data = self.settings()
        before = deepcopy(data)
        for action in (lambda: set_provider_visibility(data, "missing", False),
                       lambda: set_model_visibility(data, "missing", True),
                       lambda: set_provider_visibility(data, "legacy:model-0", True),
                       lambda: set_provider_visibility(data, "api", 0),
                       lambda: set_model_visibility(data, "model-0", "false"),
                       lambda: provider_visibility_state(data, "missing"),
                       lambda: provider_visibility_state(data, "api", kind="other"),
                       lambda: set_provider_visibility(data, STANDARD_PROVIDER, False, catalogue=[{}])):
            with self.subTest(action=action):
                with self.assertRaises(ValueError): action()
                self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
