"""
Host Trace Diagnosis CLI
=========================
Command-line entry point for the Host Trace Diagnosis Agent.

Usage:
    python cli.py analyze <trace_file> [--config config.yaml] [--output reports/]
    python cli.py detect <trace_file>
    python cli.py parse <trace_file> --output ir.parquet
    python cli.py features <ir.parquet> --output features.json
    python cli.py diagnose <features.json> --output report.html
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

# Ensure the package can be imported when run as a script
sys.path.insert(0, str(Path(__file__).parent))

from ir.schema import TraceSource, FeatureVector, DiagnosisResult

logger = logging.getLogger("host_trace_diagnosis")


def load_config(config_path: str = "") -> dict:
    """Load YAML configuration, falling back to defaults."""
    default_config_path = Path(__file__).parent / "config.yaml"
    path = Path(config_path) if config_path else default_config_path
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}


def setup_logging(config: dict):
    """Configure logging from config."""
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    log_file = log_config.get("file", "")

    handlers: list = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def cmd_detect(args, config: dict):
    """Detect trace file format."""
    from parsers.detector import FormatDetector

    detector = FormatDetector(config.get("parser", {}))
    fmt = detector.detect(args.trace_file)
    print(f"File: {args.trace_file}")
    print(f"Detected format: {fmt}")


def cmd_parse(args, config: dict):
    """Parse trace file into IR (Parquet)."""
    from parsers.detector import FormatDetector
    from ir.writer import IRWriter

    detector = FormatDetector(config.get("parser", {}))
    fmt = detector.detect(args.trace_file)
    logger.info(f"Detected format: {fmt}")

    parser = detector.get_parser(fmt, config.get("parser", {}))
    if parser is None:
        logger.error(f"No parser available for format: {fmt}")
        sys.exit(1)

    writer_config = config.get("ir", {})
    output_path = args.output or str(Path(args.trace_file).with_suffix(".parquet"))

    writer = IRWriter(output_path, writer_config)
    event_count = 0
    t0 = time.time()

    for event in parser.parse(args.trace_file):
        writer.write_event(event)
        event_count += 1
        if event_count % 100000 == 0:
            logger.info(f"Parsed {event_count} events...")

    metadata = parser.get_metadata()
    writer.finalize(metadata)
    elapsed = time.time() - t0

    logger.info(f"Parse complete: {event_count} events in {elapsed:.1f}s")
    logger.info(f"IR written to: {output_path}")
    logger.info(f"Metadata: source={metadata.source}, "
                f"duration={metadata.duration_us / 1e6:.1f}s, "
                f"devices={metadata.devices}")


def cmd_features(args, config: dict):
    """Extract features from IR."""
    from ir.reader import IRReader
    from features.host_metrics import HostMetricsExtractor
    from features.gap_scanner import GapScanner
    from features.correlation import CorrelationEngine
    from features.vector import FeatureVectorBuilder

    reader = IRReader(args.ir_file, config.get("ir", {}))
    metadata = reader.read_metadata()
    logger.info(f"Loaded IR: {metadata.total_events} events, "
                f"duration={metadata.duration_us / 1e6:.1f}s")

    feat_config = config.get("features", {})

    # Step 1: Host metrics (A + B class features)
    logger.info("Extracting host metrics...")
    host_extractor = HostMetricsExtractor(feat_config.get("cpu", {}))
    scalars, timelines = host_extractor.extract(reader)

    # Step 2: Device Gap scanning
    logger.info("Scanning device gaps...")
    gap_scanner = GapScanner(feat_config.get("gap", {}))
    gaps = gap_scanner.scan(reader)

    # Step 3: Host-Device correlation
    logger.info("Correlating host events with device gaps...")
    corr_engine = CorrelationEngine(feat_config.get("correlation", {}))
    gap_host_pairs, attribution, corr_score = corr_engine.correlate(reader, gaps)

    # Step 4: Build feature vector
    builder = FeatureVectorBuilder()
    fv = builder.build(
        scalars=scalars,
        timelines=timelines,
        gaps=gaps,
        gap_host_pairs=gap_host_pairs,
        attribution=attribution,
        corr_score=corr_score,
        metadata=metadata,
    )

    output_path = args.output or "features.json"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(fv.to_json())

    logger.info(f"Features written to: {output_path}")
    logger.info(f"Bottleneck attribution: {attribution}")
    logger.info(f"Top 3 gaps: {fv.top_gaps[:3]}")


def cmd_diagnose(args, config: dict):
    """Run diagnosis on features."""
    from features.vector import FeatureVectorBuilder
    from rules.engine import RuleEngine
    from agent.diagnosis_agent import DiagnosisAgent
    from report.generator import ReportGenerator

    # Load features
    with open(args.features_file, "r", encoding="utf-8") as f:
        fv = FeatureVectorBuilder().from_dict(json.load(f))

    logger.info(f"Loaded features: {len(fv.scalars)} scalars, "
                f"{len(fv.top_gaps)} gaps, attribution={fv.bottleneck_attribution}")

    # Step 1: Rule engine
    logger.info("Running rule engine...")
    rule_engine = RuleEngine(config.get("rules", {}))
    matched_rules = rule_engine.evaluate(fv)
    logger.info(f"Matched {len(matched_rules)} rules")

    # Step 2: LLM Agent (optional)
    agent = DiagnosisAgent(config.get("agent", {}))
    result = agent.diagnose(fv, matched_rules)

    # Step 3: Generate report
    report_gen = ReportGenerator(config.get("report", {}))
    output_dir = args.output or "reports"
    report_path = report_gen.generate(result, output_dir)

    logger.info(f"Report generated: {report_path}")
    logger.info(f"Diagnosis: {result.primary_diagnosis}")
    logger.info(f"Severity: {result.severity}")


def cmd_analyze(args, config: dict):
    """Full pipeline: parse -> features -> diagnose -> report."""
    trace_file = args.trace_file
    logger.info(f"=== Full Analysis Pipeline ===")
    logger.info(f"Trace file: {trace_file}")

    # Step 1: Parse
    ir_file = str(Path(trace_file).with_suffix(".parquet"))
    parse_args = argparse.Namespace(trace_file=trace_file, output=ir_file)
    cmd_parse(parse_args, config)

    # Step 2: Features
    features_file = "features.json"
    feat_args = argparse.Namespace(ir_file=ir_file, output=features_file)
    cmd_features(feat_args, config)

    # Step 3: Diagnose
    output_dir = args.output or "reports"
    diag_args = argparse.Namespace(features_file=features_file, output=output_dir)
    cmd_diagnose(diag_args, config)

    logger.info(f"=== Pipeline Complete ===")


def main():
    parser = argparse.ArgumentParser(
        description="Host Trace Diagnosis Agent - Locate host-side performance issues from trace data"
    )
    parser.add_argument("--config", default="", help="Path to config.yaml")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # analyze - full pipeline
    p_analyze = subparsers.add_parser("analyze", help="Full pipeline: parse -> features -> diagnose")
    p_analyze.add_argument("trace_file", help="Path to trace file")
    p_analyze.add_argument("--output", default="reports", help="Output directory for reports")

    # detect - format detection only
    p_detect = subparsers.add_parser("detect", help="Detect trace file format")
    p_detect.add_argument("trace_file", help="Path to trace file")

    # parse - parse to IR only
    p_parse = subparsers.add_parser("parse", help="Parse trace file to IR (Parquet)")
    p_parse.add_argument("trace_file", help="Path to trace file")
    p_parse.add_argument("--output", default="", help="Output IR file path")

    # features - extract features from IR
    p_features = subparsers.add_parser("features", help="Extract features from IR")
    p_features.add_argument("ir_file", help="Path to IR file (Parquet)")
    p_features.add_argument("--output", default="features.json", help="Output features JSON path")

    # diagnose - run diagnosis on features
    p_diagnose = subparsers.add_parser("diagnose", help="Run diagnosis on features")
    p_diagnose.add_argument("features_file", help="Path to features JSON")
    p_diagnose.add_argument("--output", default="reports", help="Output directory for reports")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    config = load_config(args.config)
    setup_logging(config)

    commands = {
        "analyze": cmd_analyze,
        "detect": cmd_detect,
        "parse": cmd_parse,
        "features": cmd_features,
        "diagnose": cmd_diagnose,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args, config)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
