#!/usr/bin/env python3
"""Measure the ARM Coder dogfood path without printing credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import subprocess
import time
from dataclasses import dataclass, field

FRAMES = 6
COMMAND_TIMEOUT_SECS = 180
HOURS_PER_MONTH = 730


def active_compute_cost(hourly_usd: float, active_seconds: float) -> float:
    """Return compute spend for the interval in dollars."""
    return round(hourly_usd * active_seconds / 3600, 6)


def monthly_idle_cost(
    control_hourly_usd: float,
    persistent_disk_gb: float,
    ebs_gb_month_usd: float,
) -> float:
    """Return fixed monthly control compute plus persistent gp3 storage."""
    return round(
        control_hourly_usd * HOURS_PER_MONTH + persistent_disk_gb * ebs_gb_month_usd,
        3,
    )


@dataclass
class Report:
    failures: int = 0
    measurements: dict[str, object] = field(default_factory=dict)

    def record(self, name: str, value: object) -> None:
        self.measurements[name] = value
        print(f"[ ok ] {name}: {value}")

    def fail(self, name: str, detail: str) -> None:
        self.failures += 1
        print(f"[FAIL] {name}: {detail}")


def run(argv: list[str], timeout: int = COMMAND_TIMEOUT_SECS) -> tuple[int, str]:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def aws_on_demand_hourly_price(
    aws_bin: str,
    instance_type: str,
    location: str,
) -> float | None:
    filters = {
        "instanceType": instance_type,
        "location": location,
        "operatingSystem": "Linux",
        "tenancy": "Shared",
        "preInstalledSw": "NA",
        "capacitystatus": "Used",
    }
    argv = [
        aws_bin,
        "pricing",
        "get-products",
        "--region",
        "us-east-1",
        "--service-code",
        "AmazonEC2",
    ]
    for key, value in filters.items():
        argv.extend(("--filters", f"Type=TERM_MATCH,Field={key},Value={value}"))
    argv.extend(("--output", "json"))
    rc, raw = run(argv)
    if rc:
        return None
    try:
        products = json.loads(raw)["PriceList"]
        prices: list[float] = []
        for encoded in products:
            product = json.loads(encoded) if isinstance(encoded, str) else encoded
            for term in product["terms"]["OnDemand"].values():
                for dimension in term["priceDimensions"].values():
                    price = float(dimension["pricePerUnit"]["USD"])
                    if price > 0:
                        prices.append(price)
        return min(prices) if prices else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def measure_transport(coder_bin: str, workspace: str) -> list[float]:
    process = await asyncio.create_subprocess_exec(
        coder_bin,
        "ssh",
        workspace,
        "--",
        "cat",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdin and process.stdout
    latencies: list[float] = []
    try:
        for index in range(FRAMES):
            payload = json.dumps({"jsonrpc": "2.0", "id": index, "method": "ping"}) + "\n"
            started = time.perf_counter()
            process.stdin.write(payload.encode())
            await process.stdin.drain()
            response = await asyncio.wait_for(process.stdout.readline(), timeout=30)
            if not response:
                break
            latencies.append((time.perf_counter() - started) * 1000)
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
    return latencies


async def check_acp_handshake(coder_bin: str, workspace: str) -> dict[str, object] | None:
    process = await asyncio.create_subprocess_exec(
        coder_bin,
        "ssh",
        workspace,
        "--",
        "kiro-cli",
        "acp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdin and process.stdout
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-08-22",
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
                "elicitation": {"form": {}, "url": {}},
            },
        },
    }
    try:
        process.stdin.write((json.dumps(request) + "\n").encode())
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=60)
        if not line:
            return None
        response = json.loads(line)
        result = response.get("result")
        return result if isinstance(result, dict) else None
    except (asyncio.TimeoutError, json.JSONDecodeError):
        return None
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace")
    parser.add_argument("--start", action="store_true", help="Start and time a stopped workspace.")
    parser.add_argument("--stop-after", action="store_true", help="Stop a workspace started here.")
    parser.add_argument("--workspace-instance-type", default="c8g.large")
    parser.add_argument("--control-instance-type", default="t4g.medium")
    parser.add_argument("--aws-location", default="US East (Ohio)")
    parser.add_argument("--persistent-disk-gb", type=float, default=60)
    parser.add_argument("--ebs-gb-month-usd", type=float, default=0.08)
    parser.add_argument("--active-hours", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = Report()
    coder_bin = shutil.which("coder")
    aws_bin = shutil.which("aws")
    if not coder_bin:
        report.fail("Coder CLI", "not found on PATH")
        return report.failures

    started_here = False
    if args.start:
        started = time.perf_counter()
        rc, output = run([coder_bin, "start", args.workspace, "--yes"], timeout=900)
        if rc:
            report.fail("workspace start", output[-200:])
            return report.failures
        started_here = True
        report.record("workspace_start_seconds", round(time.perf_counter() - started, 3))

    rc, architecture = run([coder_bin, "ssh", args.workspace, "--", "uname", "-m"])
    if rc or architecture.strip() != "aarch64":
        report.fail("workspace architecture", architecture or "unreachable")
    else:
        report.record("workspace_architecture", "aarch64")

    rc, version = run([coder_bin, "ssh", args.workspace, "--", "kiro-cli", "--version"])
    if rc:
        report.fail("kiro-cli", version[-200:])
    else:
        report.record("kiro_cli_version", version.splitlines()[0])

    latencies = asyncio.run(measure_transport(coder_bin, args.workspace))
    if len(latencies) < FRAMES:
        report.fail("ACP transport", f"received {len(latencies)}/{FRAMES} frames")
    else:
        report.record("transport_connect_ms", round(latencies[0], 3))
        report.record("transport_steady_median_ms", round(statistics.median(latencies[1:]), 3))

    handshake = asyncio.run(check_acp_handshake(coder_bin, args.workspace))
    if handshake is None:
        report.fail("ACP initialize", "no successful response")
    else:
        report.record("acp_protocol_version", handshake.get("protocolVersion"))

    if aws_bin:
        workspace_hourly = aws_on_demand_hourly_price(
            aws_bin, args.workspace_instance_type, args.aws_location
        )
        control_hourly = aws_on_demand_hourly_price(
            aws_bin, args.control_instance_type, args.aws_location
        )
        if workspace_hourly is not None:
            report.record("workspace_hourly_usd", workspace_hourly)
            report.record(
                "active_compute_usd",
                active_compute_cost(workspace_hourly, args.active_hours * 3600),
            )
        if control_hourly is not None:
            report.record("control_hourly_usd", control_hourly)
            report.record(
                "estimated_idle_monthly_usd",
                monthly_idle_cost(
                    control_hourly,
                    args.persistent_disk_gb,
                    args.ebs_gb_month_usd,
                ),
            )

    if started_here and args.stop_after:
        rc, output = run([coder_bin, "stop", args.workspace, "--yes"], timeout=600)
        if rc:
            report.fail("workspace stop", output[-200:])
        else:
            report.record("workspace_stopped", True)

    print(json.dumps(report.measurements, indent=2, sort_keys=True))
    return report.failures


if __name__ == "__main__":
    raise SystemExit(main())
