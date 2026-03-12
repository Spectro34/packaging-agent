"""
CLI interface for the openSUSE Packaging Agent.
"""

import argparse
import json
import sys

from packaging_agent.config import load_config
from packaging_agent.agents.orchestrator import Orchestrator


def main():
    parser = argparse.ArgumentParser(
        description="openSUSE Packaging Agent — AI-powered package maintenance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  scan                          Scan all packages for CVEs, version drift, build failures
  analyze <package>             Deep analysis of a single package
  upgrade <package> <version>   Upgrade a package to a new version
  build <work_dir>              Build a package locally from an osc checkout
  review <work_dir>             Review a spec file for quality issues
  report                        Generate a security intelligence report
  ask "<question>"              Ask a packaging question (uses AI + knowledge base)

Examples:
  %(prog)s scan
  %(prog)s analyze molecule
  %(prog)s upgrade molecule 26.3.0 --live
  %(prog)s ask "how do I package a Python project with pyproject.toml?"
""")

    parser.add_argument("command", choices=[
        "scan", "analyze", "upgrade", "build", "review", "report", "ask"],
        help="Command to execute")
    parser.add_argument("args", nargs="*", help="Command arguments")
    parser.add_argument("--live", action="store_true",
                        help="Execute live operations (branch, build, commit)")
    parser.add_argument("--project", type=str, default=None,
                        help="OBS project (overrides config)")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output results as JSON (for n8n/MCP integration)")

    args = parser.parse_args()

    # Load config
    config = load_config()
    if args.project:
        config["obs_project"] = args.project

    # Create orchestrator
    orchestrator = Orchestrator(config)

    # Build command args
    cmd_args = {}
    if args.command == "scan":
        cmd_args["project"] = config.get("obs_project")

    elif args.command == "analyze":
        if not args.args:
            parser.error("analyze requires a package name")
        cmd_args["package"] = args.args[0]
        cmd_args["project"] = config.get("obs_project")

    elif args.command == "upgrade":
        if len(args.args) < 2:
            parser.error("upgrade requires <package> <version>")
        cmd_args["package"] = args.args[0]
        cmd_args["target_version"] = args.args[1]
        cmd_args["project"] = config.get("obs_project")

    elif args.command == "build":
        if not args.args:
            parser.error("build requires a work directory path")
        cmd_args["work_dir"] = args.args[0]
        cmd_args["package"] = args.args[1] if len(args.args) > 1 else ""

    elif args.command == "review":
        if not args.args:
            parser.error("review requires a work directory path")
        cmd_args["work_dir"] = args.args[0]
        cmd_args["package"] = args.args[1] if len(args.args) > 1 else ""

    elif args.command == "report":
        cmd_args["project"] = config.get("obs_project")

    elif args.command == "ask":
        if not args.args:
            parser.error("ask requires a question string")
        cmd_args["question"] = " ".join(args.args)

    # Execute
    result = orchestrator.run(args.command, cmd_args, live=args.live)

    # Output
    if args.json_output:
        print(json.dumps(orchestrator.to_json(result), indent=2, default=str))
    elif args.command == "ask":
        print(f"\n{result.details.get('answer', result.summary)}")
    elif not result.success:
        print(f"\nFailed: {result.summary}")
        for err in result.errors:
            print(f"  Error: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
