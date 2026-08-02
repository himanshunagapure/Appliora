import argparse
import sys

from core.resume_parser import ResumeParseError, parse_resume
from utils.logger import setup_logger

logger = setup_logger("jobapplypro.cli")


def cmd_init(args: argparse.Namespace) -> int:
    """Parse a resume once into config/profile.json (Phase 2 / FR1)."""
    try:
        profile = parse_resume(resume_path=args.resume, output_path=args.output)
    except ResumeParseError as exc:
        logger.error("Resume parsing failed: %s", exc)
        print(f"\nResume parsing failed: {exc}\n", file=sys.stderr)
        return 1

    name = profile.get("contact", {}).get("name", "unknown")
    n_exp = len(profile.get("experience", []))
    n_projects = len(profile.get("projects", []))
    n_skills = len(profile.get("skills", []))
    n_facts = len(profile.get("resume_facts", []))
    output = args.output or "config/profile.json"

    print(
        f"\nParsed resume for {name}\n"
        f"   {n_exp} experience | {n_projects} projects | {n_skills} skills | "
        f"{n_facts} protected resume_facts\n"
        f"   Saved to: {output}\n"
        f"\n   Review this file before continuing — later stages trust it as ground truth.\n"
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.stage == "discover":
        # Lazy import so `init` (Phase 2) does not require JobSpy at CLI load time.
        from core.discover import run_discovery

        logger.info("Executing pipeline stage: 'discover'")
        try:
            run_discovery()
            logger.info("Stage 'discover' finished successfully.")
            return 0
        except Exception as e:
            logger.error("Pipeline stage 'discover' failed with error: %s", e, exc_info=True)
            return 1

    logger.error("Stage '%s' is not implemented yet in this version.", args.stage)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="JobApplyPro CLI - Personal job-search automation pipeline"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    init_parser = subparsers.add_parser(
        "init", help="Parse a resume once into config/profile.json"
    )
    init_parser.add_argument(
        "--resume", required=True, help="Path to resume (PDF/DOCX/TXT)"
    )
    init_parser.add_argument(
        "--output",
        default=None,
        help="Output path (default: config/profile.json)",
    )
    init_parser.set_defaults(func=cmd_init)

    run_parser = subparsers.add_parser("run", help="Run a specific stage of the pipeline")
    run_parser.add_argument(
        "--stage",
        required=True,
        choices=["discover", "score", "tailor", "cover_letter", "apply"],
        help="Pipeline stage to execute",
    )
    run_parser.set_defaults(func=cmd_run)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
