"""Menu runner: live tests, inventory, settings and setup."""

import argparse
import asyncio
import getpass
import json
from datetime import UTC, datetime
from pathlib import Path

from docling_tester.corpus import Case, Corpus
from docling_tester.menu import TITLE, choose, select_many
from docling_tester.runner import Settings, run
from docling_tester.settings import DEFAULT_PATH, SECRETS, load, save, validate


class Runner:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.settings = load(self.path)
        self.corpus = Corpus()
        self.results: dict[str, str] = {}

    def configure(self, keys: list[str] | None = None) -> None:
        keys = keys if keys is not None else list(self.settings)
        while True:
            labels = [
                key.replace("_", " ").title()
                + " · "
                + (
                    ("Saved" if self.settings[key] else "Not set")
                    if key in SECRETS
                    else str(self.settings[key]) or "Not set"
                )
                for key in keys
            ]
            index = choose("Settings — saved automatically", labels + ["Back"])
            if index == len(keys):
                return
            key = keys[index]
            values = self.settings.copy()
            if type(values[key]) is bool:
                values[key] = not values[key]
            else:
                prompt = key.replace("_", " ").title() + " (blank clears): "
                text = getpass.getpass(prompt) if key in SECRETS else input(prompt).strip()
                values[key] = int(text) if type(values[key]) is int else text
            validate(values)
            save(self.path, values)
            self.settings = values

    def launch(self, cases: list[Case]) -> None:
        if not cases:
            return
        mode_index = choose("Choose conversion mode", ["Direct Docling", "AgentGateway", "Cancel"])
        if mode_index == 2:
            return
        mode = ("direct", "gateway")[mode_index]
        values = self.settings
        local = mode == "direct" and values["direct_transport"] == "docker"
        if (not local and not values[mode + "_url"]) or (
            mode == "gateway" and not values["gateway_model"]
        ):
            print("Configure the service URL and Gateway model in Setup first.")
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        output = self.path.parent / values["output_directory"] / f"{stamp}-{mode}"
        expectation_path = values["expectations"]
        settings = Settings(
            mode,
            values[mode + "_url"],
            output,
            key=values[mode + "_api_key"],
            model=values["gateway_model"],
            auth_header=values["gateway_auth_header"],
            capture_url=values["capture_url"] if mode == "gateway" else "",
            capture_key=values["capture_api_key"],
            timeout=values["timeout"],
            pipeline="vlm" if local else values["pipeline"],
            ocr=values["ocr"],
            ocr_preset=values["ocr_preset"],
            vlm_preset=values["vlm_preset"],
            reference_profile=values["reference_profile"],
            runtime_label=values["runtime_label"],
            expectations=self.path.parent / expectation_path if expectation_path else None,
            case_ids=tuple(case.path for case in cases),
            direct_transport=values["direct_transport"],
            docker_context=values["docker_context"],
            docker_container=values["docker_container"],
        )
        report, _ = asyncio.run(run(settings, self.corpus))
        for result in report["results"]:
            self.results[result["case"]] = result["status"]
        print(json.dumps(report["counts"], indent=2))
        print(f"Report: {output / 'report.json'}")

    def category(self, name: str, cases: list[Case]) -> None:
        while True:
            labels = [
                f"{Path(case.path).name} · {self.results.get(case.path, 'Not run')}"
                for case in cases
            ]
            index = choose(name, labels + ["Select several tests", "Test all in category", "Back"])
            if index == len(cases) + 2:
                return
            if index == len(cases):
                selected = select_many(name, [case.path for case in cases])
                if not selected:
                    continue
                self.launch([cases[i] for i in selected])
            else:
                self.launch(cases if index == len(cases) + 1 else [cases[index]])
            input("\nEnter to return…")

    def live_tests(self) -> None:
        families = sorted({case.family for case in self.corpus.cases})
        while True:
            index = choose("Live tests", families + ["Test all categories", "Back"])
            if index == len(families) + 1:
                return
            if index == len(families):
                self.launch(self.corpus.cases)
                input("\nEnter to return…")
            else:
                family = families[index]
                self.category(family, [case for case in self.corpus.cases if case.family == family])

    def main(self) -> int:
        while True:
            index = choose(TITLE, ["Live tests", "Inventory", "Settings", "Setup", "Quit"])
            if index == 4:
                return 0
            try:
                if index == 0:
                    self.live_tests()
                elif index == 1:
                    print(json.dumps(self.corpus.verify(), indent=2))
                    input("\nEnter to return…")
                elif index == 2:
                    self.configure()
                else:
                    self.configure(
                        [
                            "direct_transport",
                            "docker_context",
                            "docker_container",
                            "direct_url",
                            "direct_api_key",
                            "gateway_url",
                            "gateway_api_key",
                            "gateway_model",
                            "capture_url",
                            "capture_api_key",
                        ]
                    )
            except (ValueError, OSError):
                print("Invalid settings or connection. Private details withheld.")
                input("\nEnter to return…")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)
    try:
        return Runner(args.config).main()
    except (EOFError, KeyboardInterrupt):
        return 0
    except (ValueError, OSError):
        print("Cannot load settings. Check the JSONC file and setting types.")
        return 2
