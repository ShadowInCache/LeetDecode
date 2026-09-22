"""Make one real call to each configured provider and validate the result.

    python scripts/smoke_llm.py              # every configured provider
    python scripts/smoke_llm.py --provider gemini
    python scripts/smoke_llm.py --show       # print the full translation

No database needed - this exercises only the provider adapters and the
validation gate. Each run costs one real LLM call per provider (fractions of a
cent at the default models).

Exit code is 0 only if every attempted provider returned schema-valid output.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ProviderName, get_settings  # noqa: E402
from app.llm.base import LLMError  # noqa: E402
from app.llm.prompt import SYSTEM_PROMPT, build_user_prompt  # noqa: E402
from app.llm.service import PROVIDER_FACTORIES, validate_output  # noqa: E402

SAMPLE_PROBLEM = """\
Valid Parentheses

Given a string s containing just the characters '(', ')', '{', '}', '[' and ']',
determine if the input string is valid.

An input string is valid if:
1. Open brackets must be closed by the same type of brackets.
2. Open brackets must be closed in the correct order.
3. Every close bracket has a corresponding open bracket of the same type.

Example 1:
Input: s = "()"
Output: true

Example 2:
Input: s = "(]"
Output: false

Constraints:
1 <= s.length <= 10^4
s consists of parentheses only '()[]{}'.
"""


def check(provider_name: ProviderName, *, show: bool) -> bool:
    settings = get_settings()
    label = provider_name.value
    model = settings.model_for(provider_name)

    if not settings.api_key_for(provider_name):
        print(f"  {label:8} SKIP   no API key configured")
        return True  # not a failure; the provider simply isn't in play

    print(f"  {label:8} ...    model={model}", end="\r", flush=True)
    started = time.monotonic()
    try:
        provider = PROVIDER_FACTORIES[provider_name](settings)
        raw = provider.generate(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(SAMPLE_PROBLEM),
        )
        problem = validate_output(raw)
    except LLMError as exc:
        elapsed = time.monotonic() - started
        print(f"  {label:8} FAIL   {elapsed:5.1f}s  {type(exc).__name__}: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected clearly
        elapsed = time.monotonic() - started
        print(f"  {label:8} ERROR  {elapsed:5.1f}s  {type(exc).__name__}: {exc}")
        return False

    elapsed = time.monotonic() - started
    print(
        f"  {label:8} OK     {elapsed:5.1f}s  model={model}  "
        f"notes={len(problem.important_notes)}  chars={len(raw)}"
    )

    if show:
        print()
        print("    WHAT YOU NEED TO DO")
        print(f"      {problem.what_you_need_to_do}")
        print("    INPUT")
        print(f"      {problem.input}")
        print("    OUTPUT")
        print(f"      {problem.output}")
        print("    IMPORTANT")
        for note in problem.important_notes:
            print(f"      - {note}")
        print("    EXAMPLE")
        print(f"      input:       {problem.example.input}")
        print(f"      output:      {problem.example.output}")
        print(f"      explanation: {problem.example.explanation}")
        print()

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--provider", choices=[p.value for p in ProviderName], default=None,
        help="only test this provider (default: all configured)",
    )
    parser.add_argument(
        "--show", action="store_true", help="print the full translation"
    )
    args = parser.parse_args()

    targets = (
        [ProviderName(args.provider)] if args.provider else list(ProviderName)
    )

    print(f"Smoke-testing {len(targets)} provider(s) on a real API call:\n")
    results = [check(p, show=args.show) for p in targets]

    ok = all(results)
    print(f"\n{'All providers OK.' if ok else 'One or more providers FAILED.'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
