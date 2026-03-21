from pathlib import Path


def _candidate_paths() -> list[Path]:
    current_file = Path(__file__).resolve()
    return [
        Path("/app/shared/prompts"),
        current_file.parent,
        current_file.parents[2] / "shared" / "prompts",
    ]


def _resolve_base_path() -> Path:
    for path in _candidate_paths():
        if path.exists():
            return path
    raise FileNotFoundError("Could not locate shared/prompts directory")


BASE_PATH = _resolve_base_path()


def load_prompt(agent: str, prompt_type: str) -> str:
    path = BASE_PATH / agent / f"{prompt_type}.txt"
    return path.read_text()


def render_prompt(template: str, **kwargs) -> str:
    return template.format(**kwargs)
