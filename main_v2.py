#!/usr/bin/env python
"""v2 CLI entry point for the thin-loop PhoneAgent.

Usage:
    .venv/bin/python main_v2.py "task description" --device-id X --max-steps 20 \
        --model M --base-url U --grounding-provider hybrid --lang cn --trace-dir .traces

Resolution order: CLI overrides > shell env > project .env > defaults. All flags
default to ``None`` so unset flags never clobber env-derived values.

Exit codes: success 0 / error 1 / takeover 2 / budget-or-fuse exhausted 3.

See ``AGENTS.md`` §11 for the binding contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    assemble_capabilities,
    build_capability_registry,
)
from phone_agent.v2.config import V2Config, load_project_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main_v2.py",
        description="TaskWizard thin-loop v2 PhoneAgent CLI",
    )
    parser.add_argument("task", nargs="?", default=None, help="natural-language task description")
    parser.add_argument("--device-id", default=None, help="ADB device serial")
    parser.add_argument("--max-steps", type=int, default=None, help="runaway-loop fuse: max model calls (PHONE_AGENT_MAX_STEPS, default 100)")
    parser.add_argument("--model", default=None, help="model id (PHONE_AGENT_MODEL)")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL")
    parser.add_argument("--grounding-provider", default=None, help="grounding provider name")
    parser.add_argument(
        "--marks-windowed",
        default=None,
        choices=("auto", "on", "off"),
        help="windowed marks mode (PHONE_AGENT_MARKS_WINDOWED, default auto)",
    )
    parser.add_argument("--lang", default=None, help="prompt language (cn/en)")
    parser.add_argument("--trace-dir", default=None, help="trace output directory")
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument(
        "--dream",
        action="store_true",
        help="consolidate the local App-KB instead of running a phone task",
    )
    maintenance.add_argument(
        "--rebuild-vec",
        action="store_true",
        help="rebuild the semantic recall index from episode/App-KB JSONL",
    )
    maintenance.add_argument(
        "--distill",
        action="store_true",
        help=(
            "distill episode outcomes into lessons graded auto_approved/"
            " needs_review (offline)"
        ),
    )
    maintenance.add_argument(
        "--review-lessons",
        action="store_true",
        help="interactively review proposed lessons",
    )
    maintenance.add_argument(
        "--approve-lesson",
        metavar="ID",
        help="approve one lesson (human correction channel, never a gate)",
    )
    maintenance.add_argument(
        "--revoke-lesson",
        nargs=2,
        metavar=("ID", "REASON"),
        help="revoke one lesson with a reason",
    )
    maintenance.add_argument(
        "--supersede-lesson",
        nargs=2,
        metavar=("ID", "TEXT"),
        help="create a proposed next version of a lesson",
    )
    maintenance.add_argument(
        "--learn-alias",
        metavar="NAME=PACKAGE",
        help="set a highest-trust global app alias",
    )
    maintenance.add_argument(
        "--forget-alias",
        metavar="NAME",
        help="remove global learned/user aliases for a name",
    )
    return parser


def _overrides_from_args(args: argparse.Namespace) -> dict:
    """Map CLI flags to V2Config field names; None values are dropped by from_env."""

    return {
        "device_id": args.device_id,
        "max_model_calls": args.max_steps,
        "model_name": args.model,
        "base_url": args.base_url,
        "grounding_provider": args.grounding_provider,
        "marks_windowed": args.marks_windowed,
        "lang": args.lang,
        "trace_dir": args.trace_dir,
    }


def _device_inventory(config: V2Config) -> set[str] | None:
    """Return the current installed-package set, or None when unavailable."""

    try:
        from phone_agent.device_factory import get_device_factory

        inventory = get_device_factory().get_installed_app_inventory(config.device_id)
        packages = set(getattr(inventory, "packages", ()) or ())
        return packages or None
    except Exception:  # noqa: BLE001 - dream is fail-open when the device is absent
        return None


def _run_dream(
    config: V2Config,
    *,
    light: bool,
    store: Any | None = None,
) -> dict[str, Any]:
    """Maintain App-KB and experience through the shared dream implementation."""

    from phone_agent.v2.dream import run_maintenance

    return run_maintenance(
        config,
        light=light,
        store=store,
        inventory_provider=lambda: _device_inventory(config),
    )


def _print_dream_summary(summary: dict[str, Any]) -> None:
    print(f"dream: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}")
    for entry in summary.get("lessons_demoted") or ():
        print(
            "lessons_demoted: "
            f"{entry.get('lesson_id')} ({'; '.join(entry.get('reasons') or ())})"
        )
    for entry in summary.get("suggested_revoke") or ():
        with_lesson = entry.get("runs_with") or {}
        without_lesson = entry.get("runs_without") or {}
        print(
            "suggested_revoke: "
            f"{entry.get('lesson_id')} success_rate "
            f"{with_lesson.get('success_rate', 0.0):.2f} "
            f"({with_lesson.get('runs', 0)} injected runs) < "
            f"{without_lesson.get('success_rate', 0.0):.2f} "
            f"({without_lesson.get('runs', 0)} runs without it) "
            "-- human decision, nothing was revoked"
        )


def _lesson_store(config: V2Config) -> Any:
    from phone_agent.v2.evolution import LessonStore

    return LessonStore(config.lessons_dir)


def _approve_lesson(config: V2Config, lesson_id: str) -> dict[str, Any]:
    from phone_agent.v2.evolution import approve_lesson

    return approve_lesson(_lesson_store(config), lesson_id).to_dict()


def _review_lessons(config: V2Config) -> dict[str, int]:
    """Interactively approve/revoke proposals; skip leaves the log untouched."""

    from phone_agent.v2.evolution import evaluate_promotion, read_episode_outcomes

    from phone_agent.v2.evolution import proposal_metadata

    store = _lesson_store(config)
    episodes = read_episode_outcomes(config.experience_dir)
    suggestions = _lesson_effectiveness_by_id(config)
    metadata = proposal_metadata(config.lessons_dir)
    reviewed = approved = revoked = 0
    for candidate in [
        *store.lessons(status="proposed"),
        *store.lessons(status="needs_review"),
    ]:
        # Print the stored record: evaluate_promotion rewrites the status,
        # which would hide a needs_review procedure card.
        print(json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2))
        evaluation = evaluate_promotion(candidate, episodes)
        if evaluation.reasons:
            print(
                "fact_reference: "
                + json.dumps(list(evaluation.reasons), ensure_ascii=False, sort_keys=True)
            )
        grading = metadata.get(candidate.lesson_id)
        if grading:
            print(
                "grading: "
                + json.dumps(grading, ensure_ascii=False, indent=2, sort_keys=True)
            )
        suggestion = suggestions.get(candidate.lesson_id)
        if suggestion is not None:
            print(
                "effectiveness: "
                + json.dumps(suggestion, ensure_ascii=False, sort_keys=True)
            )
        verdict = input("[a]pprove / [r]evoke / [s]kip: ").strip().lower()
        reviewed += 1
        if verdict in {"a", "approve"}:
            # Human correction channel, not a gate: the reference facts above
            # never block an explicit approval.
            store.approve(candidate.lesson_id)
            approved += 1
        elif verdict in {"r", "revoke"}:
            reason = input("reason: ").strip()
            try:
                store.revoke(candidate.lesson_id, reason)
            except ValueError as exc:
                print(f"blocked: {exc}", file=sys.stderr)
            else:
                revoked += 1
    return {"reviewed": reviewed, "approved": approved, "revoked": revoked}


def _lesson_effectiveness_by_id(config: V2Config) -> dict[str, Any]:
    """Index the injection-effectiveness report by lesson id; fail open."""

    if not getattr(config, "experience_enabled", False):
        return {}
    try:
        from phone_agent.v2.dream import lesson_effectiveness
        from phone_agent.v2.experience import load_episodes

        view = load_episodes(config.experience_dir)
        episodes = [
            record
            for record in view.values()
            if record.get("type") == "episode_outcome"
        ]
        return {
            item["lesson_id"]: item for item in lesson_effectiveness(episodes)
        }
    except Exception:  # noqa: BLE001 - review stays usable without statistics
        return {}


def _maintenance_requested(args: argparse.Namespace) -> bool:
    return bool(
        args.dream
        or args.rebuild_vec
        or args.distill
        or args.review_lessons
        or args.approve_lesson
        or args.revoke_lesson
        or args.supersede_lesson
        or getattr(args, "learn_alias", None) is not None
        or getattr(args, "forget_alias", None) is not None
    )


def _maintenance_command(args: argparse.Namespace) -> str | None:
    for name in (
        "dream",
        "rebuild_vec",
        "distill",
        "review_lessons",
        "approve_lesson",
        "revoke_lesson",
        "supersede_lesson",
        "learn_alias",
        "forget_alias",
    ):
        value = getattr(args, name, None)
        if value is not None and value is not False:
            return name
    return None


def _build_cli_capability_context(config: V2Config) -> CapabilityAssemblyContext:
    """Mount maintenance commands through the same capability registry."""

    def dream(_args: argparse.Namespace) -> int:
        _print_dream_summary(_run_dream(config, light=False))
        return 0

    def rebuild_vec(_args: argparse.Namespace) -> int:
        from phone_agent.v2.recall import rebuild_index

        print(
            "vec: "
            + json.dumps(rebuild_index(config), ensure_ascii=False, sort_keys=True)
        )
        return 0

    def distill(_args: argparse.Namespace) -> int:
        if config.evolution_mode == "off":
            print("error: PHONE_AGENT_EVOLUTION=off disables --distill", file=sys.stderr)
            return 1
        from phone_agent.v2.evolution import build_distill_model, distill_lessons

        result = distill_lessons(
            f"{config.experience_dir}/events.jsonl",
            config.lessons_dir,
            model=build_distill_model(config),
            token_budget=config.token_budget,
            appkb_dir=config.memory_dir,
        )
        print(
            "distill: "
            + json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        return 0

    def review_lessons(_args: argparse.Namespace) -> int:
        print(
            "review: "
            + json.dumps(_review_lessons(config), ensure_ascii=False, sort_keys=True)
        )
        return 0

    def approve_lesson(args: argparse.Namespace) -> int:
        try:
            lesson = _approve_lesson(config, args.approve_lesson)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print("lesson: " + json.dumps(lesson, ensure_ascii=False, sort_keys=True))
        return 0

    def revoke_lesson(args: argparse.Namespace) -> int:
        lesson_id, reason = args.revoke_lesson
        try:
            lesson = _lesson_store(config).revoke(lesson_id, reason)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            "lesson: "
            + json.dumps(lesson.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        return 0

    def supersede_lesson(args: argparse.Namespace) -> int:
        lesson_id, text = args.supersede_lesson
        try:
            lesson = _lesson_store(config).supersede(lesson_id, text)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            "lesson: "
            + json.dumps(lesson.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        return 0

    def learn_alias(args: argparse.Namespace) -> int:
        from phone_agent.v2.appkb import AppKnowledgeStore, is_valid_package_name

        raw = str(args.learn_alias or "")
        if "=" not in raw:
            print(
                "error: --learn-alias expects NAME=PACKAGE",
                file=sys.stderr,
            )
            return 1
        term, package = (part.strip() for part in raw.split("=", 1))
        if not term:
            print("error: alias name must not be empty", file=sys.stderr)
            return 1
        if not is_valid_package_name(package):
            print(f"error: invalid Android package name: {package!r}", file=sys.stderr)
            return 1

        inventory = _device_inventory(config)
        warning = None
        if inventory is None:
            warning = "device inventory unavailable; package installation not verified"
        elif package not in inventory:
            warning = (
                "package is not installed on the current device; alias was still saved"
            )
        result = AppKnowledgeStore(config.memory_dir).set_user_alias(term, package)
        receipt = {
            "action": "learn_alias",
            "changed": bool(result["changed"]),
            "term": term,
            "package": package,
            "kind": "user",
            "confidence": 1.0,
            "scope": "global",
        }
        if warning:
            receipt["warning"] = warning
        print("alias: " + json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0

    def forget_alias(args: argparse.Namespace) -> int:
        from phone_agent.v2.appkb import AppKnowledgeStore

        term = str(args.forget_alias or "").strip()
        if not term:
            print("error: alias name must not be empty", file=sys.stderr)
            return 1
        removed = AppKnowledgeStore(config.memory_dir).forget_alias(term)
        print(
            "alias: "
            + json.dumps(
                {
                    "action": "forget_alias",
                    "term": term,
                    "removed": removed,
                    "preserved_kind": "device",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    context = CapabilityAssemblyContext(
        {
            "cli_handlers": {
                "dream": dream,
                "rebuild_vec": rebuild_vec,
                "distill": distill,
                "review_lessons": review_lessons,
                "approve_lesson": approve_lesson,
                "revoke_lesson": revoke_lesson,
                "supersede_lesson": supersede_lesson,
                "learn_alias": learn_alias,
                "forget_alias": forget_alias,
            }
        }
    )
    return assemble_capabilities(build_capability_registry(config), context)


def _build_plugin_parser() -> argparse.ArgumentParser:
    """Argparse layer for ``plugin`` subcommands (logic lives in plugins.py)."""

    parser = argparse.ArgumentParser(
        prog="main_v2.py plugin",
        description="Manage external capability plugins (WP-PLUGIN-C)",
    )
    sub = parser.add_subparsers(dest="plugin_command", required=True)

    sub.add_parser("list", help="show manifest entries and their assembly state")

    p_add = sub.add_parser("add", help="install a pip package or register a local path")
    p_add.add_argument("target", help="pip package name or local plugin directory")
    scope = p_add.add_mutually_exclusive_group()
    scope.add_argument(
        "--user", dest="scope", action="store_const", const="user",
        help="write the user manifest (~/.taskwizard/profile.toml)",
    )
    scope.add_argument(
        "--project", dest="scope", action="store_const", const="project",
        help="write the project manifest (default)",
    )
    p_add.set_defaults(scope="project")

    p_remove = sub.add_parser("remove", help="uninstall a package and clean the manifest")
    p_remove.add_argument("name", help="plugin name")
    rscope = p_remove.add_mutually_exclusive_group()
    rscope.add_argument(
        "--user", dest="scope", action="store_const", const="user",
        help="operate on the user manifest",
    )
    rscope.add_argument(
        "--project", dest="scope", action="store_const", const="project",
        help="operate on the project manifest (default)",
    )
    p_remove.set_defaults(scope="project")

    p_update = sub.add_parser("update", help="pip install -U one or all package plugins")
    ug = p_update.add_mutually_exclusive_group(required=True)
    ug.add_argument("name", nargs="?", default=None, help="plugin name")
    ug.add_argument("--all", dest="all_", action="store_true", help="update every package plugin")

    p_search = sub.add_parser("search", help="search the plugin index")
    p_search.add_argument("term", nargs="?", default=None, help="optional filter term")

    return parser


def _run_plugin_cli(argv: list[str], config: V2Config) -> int:
    """Dispatch a ``plugin`` subcommand; all failures print + return non-zero."""

    from phone_agent.v2 import plugins

    args = _build_plugin_parser().parse_args(argv)
    try:
        if args.plugin_command == "list":
            rows = plugins.cmd_list(config)
            if not rows:
                print("plugins: (none configured)")
            for row in rows:
                print("plugin: " + json.dumps(row, ensure_ascii=False, sort_keys=True))
            return 0
        if args.plugin_command == "add":
            receipt = plugins.cmd_add(args.target, config=config, scope=args.scope)
            print("plugin: " + json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return 0
        if args.plugin_command == "remove":
            receipt = plugins.cmd_remove(args.name, config=config, scope=args.scope)
            print("plugin: " + json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return 0
        if args.plugin_command == "update":
            receipt = plugins.cmd_update(args.name, all_=args.all_, config=config)
            print("plugin: " + json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return 0
        if args.plugin_command == "search":
            matches = plugins.cmd_search(args.term, config=config)
            if not matches:
                print("plugins: (no matches)")
            for item in matches:
                print("plugin: " + json.dumps(item, ensure_ascii=False, sort_keys=True))
            return 0
    except plugins.PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


def _external_capabilities(config: V2Config) -> list[Any]:
    """Discover enabled external plugin specs; fail-visible via SystemExit.

    A misconfigured / unloadable enabled plugin must not silently degrade the
    run, so a load or API-gate failure ends bring-up with a clear message.
    """

    if not getattr(config, "plugins_enabled", True):
        return []
    from phone_agent.v2.plugins import PluginError, discover_external_specs

    try:
        return discover_external_specs(config)
    except PluginError as exc:
        print(f"error: plugin load failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def main(argv: list[str] | None = None) -> int:
    load_project_env()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "plugin":
        config = V2Config.from_env({})
        return _run_plugin_cli(raw_argv[1:], config)

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.task and not _maintenance_requested(args):
        parser.error("a task description is required")
    if args.task and _maintenance_requested(args):
        parser.error("task description cannot be combined with a maintenance command")

    config = V2Config.from_env(_overrides_from_args(args))
    command_name = _maintenance_command(args)
    if command_name is not None:
        handler = _build_cli_capability_context(config).cli_commands.get(command_name)
        if handler is None:
            print(
                f"error: capability for --{command_name.replace('_', '-')} is unavailable",
                file=sys.stderr,
            )
            return 1
        return int(handler(args))

    # ThinPhoneAgent is delivered by the integration/middleware workstream
    # (phone_agent/v2/agent.py). Guard the import so the CLI skeleton lands and
    # runs --help before that file exists.
    try:
        from phone_agent.v2.agent import ThinPhoneAgent
    except ImportError as exc:
        print(
            "error: phone_agent.v2.agent.ThinPhoneAgent is not available yet "
            f"(this lands at integration): {exc}",
            file=sys.stderr,
        )
        return 1

    external_capabilities = _external_capabilities(config)
    # Pass the plugin seam only when populated so injected/legacy agent
    # factories without the parameter keep working (empty = zero-diff path).
    if external_capabilities:
        agent = ThinPhoneAgent(config, extra_capabilities=external_capabilities)
    else:
        agent = ThinPhoneAgent(config)
    result = agent.run(args.task)
    if getattr(agent, "_last_dream_summary", None) is not None:
        _print_dream_summary(agent._last_dream_summary)
    elif config.app_kb_enabled and config.dream_mode == "auto":
        # Compatibility for injected/legacy agent factories without lifecycle
        # hooks.  ThinPhoneAgent itself executes this through the dream run_end
        # hook and therefore never enters this branch.
        _print_dream_summary(
            _run_dream(
                config, light=True, store=getattr(agent.session, "app_store", None)
            )
        )

    steps = getattr(result, "steps", None)
    reason = getattr(result, "reason", "")
    success = bool(getattr(result, "success", False))
    trace_path = getattr(result, "trace_path", None)

    print(f"steps={steps} reason={reason}")
    if trace_path:
        print(f"trace: {trace_path}")

    if success:
        return 0
    if reason in {"token_budget_exhausted", "loop_fuse"}:
        return 3
    if getattr(result, "reason", None) and "takeover" in str(reason).lower():
        return 2
    if getattr(result, "takeover_reason", None):
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
