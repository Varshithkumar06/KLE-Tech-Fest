#!/usr/bin/env python3
"""
Unified CLI entry point - runs all agents in one process.

Usage:
    python run_all_agents.py --text "Build a smart drone with camera"
    python run_all_agents.py --image path/to/diagram.png
    python run_all_agents.py --pdf path/to/requirements.pdf
"""

import sys
import os
import json
import argparse
import logging

# Suppress noisy google_genai AFC warnings BEFORE any imports
logging.getLogger("google_genai").setLevel(logging.WARNING)
logging.getLogger("google_genai.models").setLevel(logging.WARNING)

from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "pipeline_app"))

# Import all agent functions from the pipeline
from app import (
    run_gemini_agent,
    run_hardware_agent,
    run_software_agent,
    run_final_prediction,
    predict_ucp_effort,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def run_full_pipeline(text: str = None, image_path: str = None, pdf_path: str = None, timeout_secs: int = 60) -> dict:
    """Run all agents sequentially and return combined results."""
    logger.info("=" * 60)
    logger.info("STARTING UNIFIED PIPELINE")
    logger.info("=" * 60)
    
    # STAGE 1: Gemini Agent
    logger.info("[1/4] Running Gemini Agent (architecture breakdown)...")
    
    config = types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=2048,
        tools=gemini_tool_definitions,
    )
    chat = client.chats.create(model="gemini-1.5-flash-latest")
    response = chat.send_message(prompt, config=config)
    gemini_out = {
        "hw_items": response.hardware_items,
        "sw_items": response.software_items,
        "team": response.team,
        "effort_inputs": {"prompt": text or ""}
    }
    logger.info(f"     Hardware items: {len(gemini_out['hw_items'])}")
    logger.info(f"     Software items: {len(gemini_out['sw_items'])}")
    logger.info(f"     Team roles: {len(gemini_out['team'])}")
    
    # STAGE 2a: Hardware Agent (parallel with software)
    logger.info("[2/4] Running Hardware Agent (live price search)...")
    hw_out = run_hardware_agent(gemini_out["hw_items"])
    logger.info(f"     Items priced: {len(hw_out.get('items', []))}")
    logger.info(f"     Total range: ₹{hw_out.get('total',{}).get('min',0):,.0f} – ₹{hw_out.get('total',{}).get('max',0):,.0f}")
    
    # STAGE 2b: Software Agent
    logger.info("[3/4] Running Software Agent (effort + licence estimation)...")
    sw_out = run_software_agent(
        gemini_out["sw_items"],
        gemini_out["team"],
        gemini_out.get("effort_inputs", {})
    )
    effort = sw_out.get("effort", {})
    logger.info(f"     Items processed: {len(sw_out.get('items', []))}")
    logger.info(f"     Dev effort: {effort.get('total_hours_likely', 0):,} hrs (~{effort.get('months_likely', '?')} months)")
    logger.info(f"     Licence cost: ₹{sw_out.get('licence_cost',{}).get('min',0):,.0f} – ₹{sw_out.get('licence_cost',{}).get('max',0):,.0f}")
    
    # STAGE 3: Final Prediction
    logger.info("[4/4] Running Final Prediction Agent (cost synthesis)...")
    prediction = run_final_prediction(gemini_out, hw_out, sw_out)
    total = prediction.get("total_cost_inr", {})
    logger.info(f"     Optimistic: ₹{total.get('optimistic', 0):,.0f}")
    logger.info(f"     Likely:     ₹{total.get('likely', 0):,.0f}")
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
    parser = argparse.ArgumentParser(description="Run all cost estimation agents in one process")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", type=str, help="Project description text")
    group.add_argument("--image", type=str, help="Path to image file (diagram, chart, etc.)")
    group.add_argument("--pdf", type=str, help="Path to PDF requirements document")
    parser.add_argument("--output", type=str, help="Output JSON file path")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress logs")
    
    args = parser.parse_args()
    
    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)
    
    # Validate file paths
    if args.image and not os.path.isfile(args.image):
        logger.error(f"Image file not found: {args.image}")
        sys.exit(1)
    if args.pdf and not os.path.isfile(args.pdf):
        logger.error(f"PDF file not found: {args.pdf}")
        sys.exit(1)
    
    try:
        result = run_full_pipeline(
            text=args.text,
            image_path=args.image,
            pdf_path=args.pdf,
        )
        
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            logger.info(f"Results saved to {args.output}")
        else:
            # Print summary to stdout
            print(json.dumps({
                "summary": result["prediction"].get("summary", ""),
                "total_cost_inr": result["prediction"].get("total_cost_inr", {}),
                "hardware_count": len(result["gemini"]["hw_items"]),
                "software_count": len(result["gemini"]["sw_items"]),
                "team_count": len(result["gemini"]["team"]),
            }, indent=2))
            
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
