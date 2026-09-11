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

    if args.stage == "score":
        from core.score import ScoringError, run_scoring_stage

        logger.info("Executing pipeline stage: 'score'")
        try:
            summary = run_scoring_stage()
        except ScoringError as exc:
            logger.error("Scoring stage aborted: %s", exc)
            print(f"\nScoring stage aborted: {exc}\n", file=sys.stderr)
            return 1
        except Exception as e:
            logger.error("Pipeline stage 'score' failed with error: %s", e, exc_info=True)
            return 1

        print(
            f"\nScoring stage complete\n"
            f"   Processed:       {summary['processed']}\n"
            f"   Scored:          {summary['scored']}\n"
            f"   Above threshold: {summary['above_threshold']}\n"
            f"   Failed:          {summary['failed']}\n"
        )
        return 0

    if args.stage == "tailor":
        from core.tailor import TailoringError, run_tailoring_stage

        logger.info("Executing pipeline stage: 'tailor'")
        try:
            summary = run_tailoring_stage()
        except TailoringError as exc:
            logger.error("Tailoring stage aborted: %s", exc)
            print(f"\nTailoring stage aborted: {exc}\n", file=sys.stderr)
            return 1
        except Exception as e:
            logger.error("Pipeline stage 'tailor' failed with error: %s", e, exc_info=True)
            return 1

        print(
            f"\nTailoring stage complete\n"
            f"   Processed:      {summary['processed']}\n"
            f"   Tailored:       {summary['tailored']}\n"
            f"   Manual review:  {summary['manual_review']}\n"
            f"   Failed:         {summary['failed']}\n"
        )
        return 0

    if args.stage == "cover_letter":
        from core.cover_letter import CoverLetterError, run_cover_letter_stage

        logger.info("Executing pipeline stage: 'cover_letter'")
        try:
            summary = run_cover_letter_stage()
        except CoverLetterError as exc:
            logger.error("Cover letter stage aborted: %s", exc)
            print(f"\nCover letter stage aborted: {exc}\n", file=sys.stderr)
            return 1
        except Exception as e:
            logger.error(
                "Pipeline stage 'cover_letter' failed with error: %s", e, exc_info=True
            )
            return 1

        print(
            f"\nCover letter stage complete\n"
            f"   Processed:           {summary['processed']}\n"
            f"   Cover letter ready:  {summary['cover_letter_ready']}\n"
            f"   Manual review:       {summary['manual_review']}\n"
            f"   Failed:              {summary['failed']}\n"
        )
        return 0

    if args.stage == "apply":
        if args.dry_run and args.submit:
            print("Use only one of --dry-run or --submit.", file=sys.stderr)
            return 1

        from core.apply import ApplyError, run_apply_stage

        dry_run = not args.submit
        if args.dry_run:
            dry_run = True
        if args.submit:
            dry_run = False

        mode = "submit" if args.submit else "dry-run"
        logger.info(
            "Executing pipeline stage: 'apply' (%s, job_id=%s)", mode, args.job_id
        )
        try:
            summary = run_apply_stage(
                dry_run=dry_run,
                submit=bool(args.submit),
                job_id=args.job_id,
            )
        except ApplyError as exc:
            logger.error("Apply stage aborted: %s", exc)
            print(f"\nApply stage aborted: {exc}\n", file=sys.stderr)
            return 1
        except Exception as e:
            logger.error("Pipeline stage 'apply' failed with error: %s", e, exc_info=True)
            return 1

        print(
            f"\nApply stage complete ({mode})\n"
            f"   Processed:      {summary['processed']}\n"
            f"   Apply pending:  {summary['apply_pending']}\n"
            f"   Applied:        {summary['applied']}\n"
            f"   Manual review:  {summary['manual_review']}\n"
            f"   Failed:         {summary['failed']}\n"
            f"   Skipped:        {summary['skipped']}\n"
            f"   Tier 2 agent:   {summary.get('tier2_attempts', 0)}\n"
        )
        return 0

    logger.error("Stage '%s' is not implemented yet in this version.", args.stage)
    return 1


def cmd_review(args: argparse.Namespace) -> int:
    """Show apply_pending jobs and approve/reject them (Phase 8)."""
    from core.review import (
        ReviewError,
        approve_job,
        format_review_card,
        list_pending_reviews,
        open_screenshot,
        reject_job,
        run_interactive_review,
    )

    if args.approve and args.reject:
        print("Use only one of --approve or --reject.", file=sys.stderr)
        return 1
    if (args.approve or args.reject) and args.job_id is None:
        print("--approve / --reject requires --job-id <id>.", file=sys.stderr)
        return 1

    try:
        if args.approve:
            item = approve_job(args.job_id)
            print(f"\nApproved job {item.job_id} ({item.company} — {item.title})")
            print(
                "Submit with:\n"
                f"  python cli.py run --stage apply --submit --job-id {item.job_id}\n"
            )
            return 0
        if args.reject:
            item = reject_job(args.job_id, reason=args.reason)
            print(
                f"\nRejected job {item.job_id} ({item.company} — {item.title}) "
                f"→ status=skipped\n"
            )
            return 0
        if args.interactive:
            summary = run_interactive_review(
                job_id=args.job_id,
                open_screenshots=args.open,
            )
            print(
                f"\nReview session: shown={summary['shown']} "
                f"approved={summary['approved']} rejected={summary['rejected']} "
                f"skipped={summary['skipped']}\n"
            )
            return 0

        items = list_pending_reviews(job_id=args.job_id)
        if not items:
            if args.job_id is not None:
                print(f"No apply_pending job with id={args.job_id}.")
            else:
                print("No apply_pending jobs. Run apply --dry-run first.")
            return 0

        print(f"\n{len(items)} apply_pending job(s) awaiting review\n")
        for i, item in enumerate(items, 1):
            print("=" * 72)
            print(f"[{i}/{len(items)}]")
            print(format_review_card(item))
            print()
            if args.open:
                open_screenshot(item.screenshot_path)

        print(
            "Approve:  python cli.py review --approve --job-id <id>\n"
            "Reject:   python cli.py review --reject --job-id <id>\n"
            "Submit:   python cli.py run --stage apply --submit --job-id <id>\n"
            "Walkthrough: python cli.py review --interactive\n"
        )
        return 0
    except ReviewError as exc:
        logger.error("Review aborted: %s", exc)
        print(f"\nReview aborted: {exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.error("Review command failed: %s", exc, exc_info=True)
        print(f"\nReview failed: {exc}\n", file=sys.stderr)
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
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="For apply stage: fill forms and screenshot without submitting (default behavior)",
    )
    run_parser.add_argument(
        "--submit",
        action="store_true",
        default=False,
        help="For apply stage: actually submit (requires --job-id of an *approved* apply_pending job)",
    )
    run_parser.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="Limit apply stage to a single job id",
    )
    run_parser.set_defaults(func=cmd_run)

    review_parser = subparsers.add_parser(
        "review",
        help="Review apply_pending jobs (screenshot + materials); approve or reject",
    )
    review_parser.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="Limit to a single job id",
    )
    review_parser.add_argument(
        "--approve",
        action="store_true",
        default=False,
        help="Approve the job (requires --job-id) so --submit may run",
    )
    review_parser.add_argument(
        "--reject",
        action="store_true",
        default=False,
        help="Reject the job (requires --job-id); status becomes skipped",
    )
    review_parser.add_argument(
        "--reason",
        default=None,
        help="Optional reject reason stored on the job",
    )
    review_parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        default=False,
        help="Walk each pending job and prompt approve/reject/skip",
    )
    review_parser.add_argument(
        "--open",
        action="store_true",
        default=False,
        help="Open the screenshot in the default image viewer",
    )
    review_parser.set_defaults(func=cmd_review)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
