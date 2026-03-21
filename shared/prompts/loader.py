from pathlib import Path

BASE_PATH = Path("/app/shared/prompts")

def load_prompt(agent, prompt_type):
    path = BASE_PATH / agent / f"{prompt_type}.txt"
    return path.read_text()


def render_prompt(template: str, **kwargs):
    return template.format(**kwargs)