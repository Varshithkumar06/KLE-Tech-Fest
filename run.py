#!/usr/bin/env python3
"""
Estimo - Unified Project Cost Estimator Runner
==============================================
Usage:
    Web Application (default):
        python run.py

    Command-line Pipeline Execution:
        python run.py --text "Build a smart drone with crop disease detection"
        python run.py --image path/to/architecture_diagram.png
        python run.py --pdf path/to/requirements.pdf
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from estimo.app import (
    app,
    run_gemini_agent,
    run_hardware_agent,
    run_software_agent,
    run_final_prediction,
)
from estimo.config import PORT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("estimo")


def run_cli_pipeline(text: str = None, image_path: str = None, pdf_path: str = None) -> dict:
    """Execute the full 3-stage estimation pipeline from the CLI."""
    logger.info("=" * 60)
    logger.info("STARTING ESTIMATION PIPELINE")
    logger.info("=" * 60)

    # 1. Gemini Architecture Breakdown
    logger.info("[1/3] Gemini Vision & Systems Architect...")
    gemini_out = run_gemini_agent(text=text, image_path=image_path, pdf_path=pdf_path)
    logger.info(f"     Identified {len(gemini_out.get('hw_items', []))} HW parts, "
                f"{len(gemini_out.get('sw_items', []))} SW modules, "
                f"{len(gemini_out.get('team', []))} team roles.")

    # 2. Hardware & Software Agents in Parallel
    logger.info("[2/3] Hardware Agent (live pricing) & Software Agent (effort model)...")
    hw_out = run_hardware_agent(gemini_out.get("hw_items", []))
    sw_out = run_software_agent(
        gemini_out.get("sw_items", []),
        gemini_out.get("team", []),
        gemini_out.get("effort_inputs", {}),
    )
    hw_total = hw_out.get("total", {})
    logger.info(f"     HW cost range: ₹{hw_total.get('min', 0):,.0f} – ₹{hw_total.get('max', 0):,.0f}")
    sw_effort = sw_out.get("effort", {})
    logger.info(f"     SW dev effort: {sw_effort.get('total_hours_likely', 0):,} hrs "
                f"(~{sw_effort.get('months_likely', '?')} months)")

    # 3. Final Cost Synthesis
    logger.info("[3/3] Final Cost Prediction Synthesis...")
    prediction = run_final_prediction(gemini_out, hw_out, sw_out)
    total = prediction.get("total_cost_inr", {})
    logger.info(f"     Optimistic:  ₹{total.get('optimistic', 0):,.0f}")
    logger.info(f"     Likely:      ₹{total.get('likely', 0):,.0f}")
    logger.info(f"     Pessimistic: ₹{total.get('pessimistic', 0):,.0f}")
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info("=" * 60)

    return {
        "gemini": gemini_out,
        "hardware": hw_out,
        "software": sw_out,
        "prediction": prediction,
    }


def main():
    parser = argparse.ArgumentParser(description="Estimo - Unified Project Cost Estimator")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--text", type=str, help="Project description text")
    group.add_argument("--image", type=str, help="Path to architecture diagram image")
    group.add_argument("--pdf", type=str, help="Path to requirements PDF document")
    
    parser.add_argument("--port", type=int, default=PORT, help=f"Web server port (default: {PORT})")
    parser.add_argument("--output", type=str, help="Save CLI estimation result as JSON to this path")

    args = parser.parse_args()

    # If any pipeline input is specified, run CLI mode
    if args.text or args.image or args.pdf:
        if args.image and not os.path.isfile(args.image):
            logger.error(f"Image file not found: {args.image}")
            sys.exit(1)
        if args.pdf and not os.path.isfile(args.pdf):
            logger.error(f"PDF file not found: {args.pdf}")
            sys.exit(1)

        result = run_cli_pipeline(text=args.text, image_path=args.image, pdf_path=args.pdf)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            logger.info(f"Results saved to {args.output}")
        else:
            print(json.dumps({
                "summary": result["prediction"].get("summary", ""),
                "total_cost_inr": result["prediction"].get("total_cost_inr", {}),
                "hardware_items": len(result["gemini"].get("hw_items", [])),
                "software_items": len(result["gemini"].get("sw_items", [])),
                "team_members": len(result["gemini"].get("team", [])),
            }, indent=2))
    else:
        # Default: Start web application
        logger.info(f"Starting Estimo Web Application on http://127.0.0.1:{args.port} ...")
        app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
