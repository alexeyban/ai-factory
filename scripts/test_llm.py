import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.llm import call_llm, load_llm_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test the shared LLM adapter.")
    parser.add_argument("--model", default="opencode/bigpickle")
    parser.add_argument("--provider")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--system", default="You are a concise coding assistant.")
    parser.add_argument("--prompt", default="Return a one-line hello message for this repo.")
    args = parser.parse_args()

    config = load_llm_config(
        model=args.model,
        provider=args.provider,
        base_url=args.base_url,
        api_key=args.api_key,
    )
    print("Resolved config:")
    print(json.dumps(config.__dict__, indent=2))
    print("\nResponse:")
    print(
        call_llm(
            args.system,
            args.prompt,
            model=args.model,
            provider=args.provider,
            base_url=args.base_url,
            api_key=args.api_key,
        )
    )


if __name__ == "__main__":
    main()
