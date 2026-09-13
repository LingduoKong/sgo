import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from datasets import Dataset
from starlette.requests import Request

import web.app as appmod
import persona_loader


class DatasetRegistryTests(unittest.TestCase):
    def setUp(self):
        self._old = dict(appmod._nemotron_cache)
        self._old_dataset_paths = dict(appmod._dataset_paths)
        self._old_search_paths = list(appmod.NEMOTRON_SEARCH_PATHS)
        appmod._nemotron_cache.clear()
        appmod._dataset_paths.clear()
        appmod.NEMOTRON_SEARCH_PATHS = []

    def tearDown(self):
        appmod._nemotron_cache.clear()
        appmod._nemotron_cache.update(self._old)
        appmod._dataset_paths.clear()
        appmod._dataset_paths.update(self._old_dataset_paths)
        appmod.NEMOTRON_SEARCH_PATHS = self._old_search_paths

    def test_config_reports_truthful_dataset_metadata(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            base = Path(tmp)
            root = base / "nemotron"
            root.mkdir()
            info = root / "dataset_info.json"
            info.write_text(json.dumps({"description": "nvidia/Nemotron-Personas-USA"}))
            (root / "state.json").write_text(json.dumps({"_data_files": [{"filename": "data-00000-of-00001.arrow"}]}))
            (root / "data-00000-of-00001.arrow").write_bytes(b"arrow")
            with patch.object(appmod, "DATASET_ROOT", base):
                config = asyncio.run(appmod.get_config())

        datasets = {item["id"]: item for item in config["persona_datasets"]}
        self.assertIn("USA", datasets)
        self.assertIn("generated", datasets)
        self.assertTrue(datasets["USA"]["ready"])
        self.assertEqual(datasets["USA"]["count"], None)
        self.assertFalse(datasets["Japan"]["ready"])

    def test_country_path_is_separate_from_legacy_usa_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usa = root / "nemotron"
            usa.mkdir()
            (usa / "dataset_info.json").write_text(
                json.dumps({"description": "nvidia/Nemotron-Personas-USA"})
            )
            with patch.object(appmod, "DATASET_ROOT", root):
                self.assertEqual(appmod.dataset_path("USA"), usa)
                self.assertEqual(appmod.dataset_path("Japan"), root / "nemotron-Japan")

    def test_mismatched_country_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nemotron-Japan"
            path.mkdir()
            (path / "dataset_info.json").write_text(
                json.dumps({"description": "nvidia/Nemotron-Personas-USA"})
            )
            with self.assertRaises(appmod.DatasetIdentityError):
                appmod.validate_dataset_identity(path, "Japan")

    def test_cache_key_includes_country_and_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = {}
            for country in ("USA", "Japan"):
                path = base / country
                path.mkdir()
                (path / "dataset_info.json").write_text(
                    json.dumps({"dataset_name": f"nemotron-personas-{country.lower()}"})
                )
                (path / "state.json").write_text(json.dumps({"_data_files": [{"filename": "data.arrow"}]}))
                (path / "data.arrow").write_bytes(b"arrow")
                paths[country] = path
            calls = []
            class FakeDataset:
                def __len__(self):
                    return 1
            fake_loader = type("Loader", (), {"load_personas": lambda self, data_dir: calls.append(str(data_dir)) or FakeDataset()})()
            with patch.object(appmod, "_lazy_persona_loader", return_value=fake_loader), patch.object(
                appmod, "_dataset_paths", paths
            ):
                appmod.get_nemotron("USA")
                appmod.get_nemotron("Japan")
                appmod.get_nemotron("USA")
            self.assertEqual(calls, [str(paths["USA"]), str(paths["Japan"])])

    def test_cohort_config_keeps_optional_dataset_for_compatibility(self):
        cfg = appmod.CohortConfig(
            description="software buyers", segments=[{"label": "CTO", "count": 2}]
        )
        self.assertIsNone(cfg.dataset)

    def test_explicit_unavailable_country_fails_before_llm(self):
        cfg = appmod.CohortConfig(
            description="buyers", segments=[{"label": "buyers", "count": 1}], dataset="Japan"
        )
        with patch.object(appmod, "find_nemotron_path", return_value=None), patch.object(
            appmod, "llm_from_request", side_effect=AssertionError("LLM must not be called")
        ):
            with self.assertRaises(appmod.HTTPException) as ctx:
                asyncio.run(appmod.generate_cohort_endpoint(cfg, None))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_generated_selection_bypasses_installed_usa(self):
        cfg = appmod.CohortConfig(
            description="buyers", segments=[{"label": "buyers", "count": 1}], dataset="generated"
        )
        fake_client = object()
        with patch.object(appmod, "llm_from_request", return_value=(fake_client, "test")), patch.object(
            appmod, "generate_segment", return_value=[{"name": "A"}]
        ), patch.object(appmod, "find_nemotron_path", side_effect=AssertionError("USA must be bypassed")):
            result = asyncio.run(appmod.generate_cohort_endpoint(cfg, None))
        self.assertEqual(result["dataset"], "generated")
        self.assertEqual(result["source"], "llm-generated")

    def test_mismatched_setup_leaves_existing_folder_untouched(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            path = Path(tmp) / "japan"
            path.mkdir()
            info = path / "dataset_info.json"
            original = json.dumps({"dataset_name": "nemotron-personas-usa"})
            info.write_text(original)
            with patch.object(appmod, "PROJECT_ROOT", Path(tmp)):
                with self.assertRaises(appmod.HTTPException) as ctx:
                    asyncio.run(appmod.setup_nemotron(appmod.NemotronPathInput(dataset="Japan", path=str(path))))
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(info.read_text(), original)

    def test_filter_personas_supports_japan_native_geography_and_exact_sex(self):
        ds = Dataset.from_list([
            {"sex": "男", "age": 32, "prefecture": "東京都", "region": "関東", "area": "多摩"},
            {"sex": "女", "age": 32, "prefecture": "大阪府", "region": "関西", "area": "北摂"},
        ])

        filtered = persona_loader.filter_personas(
            ds,
            {"sex": "男", "prefecture": "東京都", "region": "関東", "area": "多摩"},
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(persona_loader.to_profile(filtered[0], 0, dataset="Japan")["country"], "Japan")

    def test_filter_personas_supports_france_native_geography(self):
        ds = Dataset.from_list([
            {"sex": "Femme", "age": 28, "commune": "Lyon", "departement": "Rhône"},
            {"sex": "Homme", "age": 28, "commune": "Paris", "departement": "Paris"},
        ])

        filtered = persona_loader.filter_personas(
            ds, {"commune": "Lyon", "departement": "Rhône"}
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["commune"], "Lyon")

    def test_filter_personas_excludes_missing_age_when_age_is_requested(self):
        ds = Dataset.from_list([{"sex": "Male", "age": None}, {"sex": "Female", "age": 30}])

        filtered = persona_loader.filter_personas(ds, {"age_min": 25, "age_max": 35})

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["sex"], "Female")

    def test_setup_rejects_incomplete_existing_dataset_with_actionable_message(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            path = Path(tmp) / "japan"
            path.mkdir()
            (path / "dataset_info.json").write_text(
                json.dumps({"dataset_name": "nemotron-personas-japan"})
            )
            with patch.object(appmod, "PROJECT_ROOT", Path(tmp)):
                with self.assertRaises(appmod.HTTPException) as ctx:
                    asyncio.run(appmod.setup_nemotron(
                        appmod.NemotronPathInput(dataset="Japan", path=str(path))
                    ))
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("incomplete", str(ctx.exception.detail).lower())

    def test_setup_rejects_concurrent_same_path_without_touching_data(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            path = Path(tmp) / "japan"
            lock = appmod._setup_lock_for(path.resolve())
            self.assertTrue(lock.acquire(blocking=False))
            try:
                with self.assertRaises(appmod.HTTPException) as ctx:
                    asyncio.run(appmod.setup_nemotron(
                        appmod.NemotronPathInput(dataset="Japan", path=str(path))
                    ))
                self.assertEqual(ctx.exception.status_code, 409)
                self.assertIn("already in progress", str(ctx.exception.detail))
            finally:
                lock.release()

    def test_dataset_cache_isolated_by_country_and_path(self):
        usa_path = Path("/tmp/sgo-cache-usa")
        japan_path = Path("/tmp/sgo-cache-japan")
        class FakeDataset:
            def __len__(self):
                return 1
        fake_usa = FakeDataset()
        fake_japan = FakeDataset()
        with patch.object(appmod, "validate_dataset_identity", return_value={}), \
             patch.object(appmod, "_lazy_persona_loader") as loader:
            loader.return_value.load_personas.side_effect = [fake_usa, fake_japan]
            self.assertIs(appmod.get_nemotron("USA", usa_path), fake_usa)
            self.assertIs(appmod.get_nemotron("Japan", japan_path), fake_japan)
            self.assertEqual(loader.return_value.load_personas.call_count, 2)

    def test_extract_filters_prompt_uses_selected_dataset_columns_and_values(self):
        class Message:
            content = '{"filters":{"sex":"男","region":"関東"},"evidence":{"sex":"men","region":"Kanto"}}'

        class Response:
            choices = [type("Choice", (), {"message": Message()})()]

        class Client:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        Client.prompt = kwargs["messages"][0]["content"]
                        return Response()

        result = appmod.extract_filters(
            Client(), "test", "men in Kanto", dataset="Japan",
            columns={"sex", "region", "prefecture"},
            sample_values={"sex": ["男", "女"], "region": ["関東", "関西"]},
        )

        self.assertEqual(result, {"sex": "男", "region": "関東"})
        self.assertIn("Japan", Client.prompt)
        self.assertIn("男", Client.prompt)
        self.assertIn("region", Client.prompt)

    def test_bias_audit_stream_runs_all_probes_with_selected_model(self):
        scope = {
            "type": "http", "method": "GET", "path": "/", "headers": [],
            "client": ("127.0.0.1", 1001), "query_string": b"",
            "scheme": "http", "server": ("test", 80),
        }
        request = Request(scope)
        request.state.api_key = ""
        request.state.base_url = ""
        request.state.model = ""
        sid = "audit-valid"
        appmod.sessions[sid] = {"cohort": [{"name": "A"}], "entity_text": "draft"}
        seen_models = []

        def paired(client, model, *args):
            seen_models.append(model)
            return [{"evaluator": "A", "delta": 1}]

        async def collect():
            response = await appmod.bias_audit_stream(
                sid, request, probes="framing,authority,order", sample=1, parallel=1
            )
            return [item async for item in response.body_iterator]

        try:
            with patch.object(appmod, "_llm_from_params", return_value=(object(), "selected-model")), \
                 patch.object(appmod, "reframe_entity", side_effect=lambda *args: args[-1]), \
                 patch.object(appmod, "run_paired_evaluation", side_effect=paired), \
                 patch.object(appmod, "generate_report", return_value="report"):
                events = asyncio.run(collect())
        finally:
            appmod.sessions.pop(sid, None)

        complete = next(json.loads(item["data"]) for item in events if item["event"] == "complete")
        self.assertNotIn("error", complete)
        self.assertEqual(len([item for item in events if item["event"] == "probe_complete"]), 3)
        self.assertEqual(seen_models, ["selected-model", "selected-model", "selected-model"])

    def test_evaluate_stream_reports_error_when_all_evaluators_fail(self):
        scope = {
            "type": "http", "method": "GET", "path": "/", "headers": [],
            "client": ("127.0.0.1", 1003), "query_string": b"",
            "scheme": "http", "server": ("test", 80),
        }
        request = Request(scope)
        request.state.api_key = ""
        request.state.base_url = ""
        request.state.model = ""
        sid = "eval-failed"
        appmod.sessions[sid] = {
            "cohort": [{"name": "A"}], "entity_text": "draft", "dataset": None,
        }

        async def collect():
            response = await appmod.evaluate_stream(sid, request, parallel=1)
            return [item async for item in response.body_iterator]

        try:
            with patch.object(appmod, "_llm_from_params", return_value=(object(), "selected-model")), \
                 patch.object(appmod, "evaluate_one", return_value={"error": "provider failed"}):
                events = asyncio.run(collect())
        finally:
            appmod.sessions.pop(sid, None)

        complete = next(json.loads(item["data"]) for item in events if item["event"] == "complete")
        self.assertIn("error", complete)
        self.assertIn("No valid", complete["error"])

    def test_india_setup_uses_default_config_and_en_in_split(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            ds = Dataset.from_list([{"sex": "Male", "age": 23, "state": "Gujarat", "district": "Ahmadabad"}])
            ds.info.dataset_name = "nemotron-personas-india"
            ds._split = "en_IN"
            with patch.object(appmod, "PROJECT_ROOT", root), patch("datasets.load_dataset", return_value=ds) as load:
                result = asyncio.run(appmod.setup_nemotron(
                    appmod.NemotronPathInput(dataset="India", path=str(root / "india"))
                ))
            self.assertEqual(result["status"], "downloaded")
            load.assert_called_once_with("nvidia/Nemotron-Personas-India", "default", split="en_IN")

    def test_india_hindi_saved_folder_is_not_ready_as_english(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            path = root / "nemotron-India"
            path.mkdir()
            (path / "dataset_info.json").write_text(json.dumps({"dataset_name": "nemotron-personas-india"}))
            (path / "state.json").write_text(json.dumps({"_split": "hi_Deva_IN", "_data_files": [{"filename": "data.arrow"}]}))
            (path / "data.arrow").write_bytes(b"arrow")
            with patch.object(appmod, "DATASET_ROOT", root), patch.object(appmod, "_dataset_paths", {}):
                self.assertIsNone(appmod.find_nemotron_path("India"))
                with self.assertRaises(appmod.HTTPException) as ctx:
                    asyncio.run(appmod.setup_nemotron(
                        appmod.NemotronPathInput(dataset="India", path=str(path))
                    ))
                self.assertEqual(ctx.exception.status_code, 409)
                self.assertIn("hi_Deva_IN", str(ctx.exception.detail))

    def test_india_en_in_count_is_used_in_config_metadata(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            path = root / "nemotron-India"
            path.mkdir()
            (path / "dataset_info.json").write_text(json.dumps({
                "dataset_name": "nemotron-personas-india",
                "splits": {"en_IN": {"num_examples": 2, "dataset_name": "nemotron-personas-india"}},
            }))
            (path / "state.json").write_text(json.dumps({"_split": "en_IN", "_data_files": [{"filename": "data.arrow"}]}))
            (path / "data.arrow").write_bytes(b"arrow")
            with patch.object(appmod, "DATASET_ROOT", root), patch.object(appmod, "_dataset_paths", {}):
                config = asyncio.run(appmod.get_config())
            record = next(item for item in config["persona_datasets"] if item["id"] == "India")
            self.assertTrue(record["ready"])
            self.assertEqual(record["count"], 2)

    def test_profile_geography_falls_back_to_singapore_and_brazil_fields(self):
        singapore = persona_loader.to_profile({"planning_area": "Sengkang", "country": "Singapore"}, 0)
        brazil = persona_loader.to_profile({"municipality": "Lyon", "country": "Brasil"}, 1)
        self.assertEqual(singapore["city"], "Sengkang")
        self.assertEqual(brazil["city"], "Lyon")


if __name__ == "__main__":
    unittest.main()
