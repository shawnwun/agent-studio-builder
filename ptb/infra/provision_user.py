#!/usr/bin/env python3
"""
Provision a dedicated Fargate container for a single Prompt to Build user.

What this does:
  1. Generates an API key for the user (or you can pass one)
  2. Stores all secrets in AWS Secrets Manager
  3. Registers an ECS task definition for the user
  4. Launches a Fargate task (dedicated container)
  5. Waits for it to be running and prints the public URL

Usage:
  python3 infra/provision_user.py \\
    --user alice \\
    --anthropic-key sk-ant-... \\
    --polyai-key <key>            # available next week

  # Tear down:
  python3 infra/provision_user.py --user alice --destroy
"""

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

import boto3

# ── Load config written by setup_aws.sh ────────────────────────────────────
CONFIG_FILE = Path(__file__).parent / ".aws_config"

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit("❌ Run infra/setup_aws.sh first to create AWS infrastructure.")
    cfg = {}
    for line in CONFIG_FILE.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg

# ── AWS clients ────────────────────────────────────────────────────────────
def clients(region: str):
    return {
        "ecs":    boto3.client("ecs",              region_name=region),
        "sm":     boto3.client("secretsmanager",   region_name=region),
        "ec2":    boto3.client("ec2",              region_name=region),
        "logs":   boto3.client("logs",             region_name=region),
    }

# ── Secrets Manager ────────────────────────────────────────────────────────
def put_secret(sm, name: str, value: dict) -> str:
    payload = json.dumps(value)
    try:
        sm.get_secret_value(SecretId=name)
        sm.update_secret(SecretId=name, SecretString=payload)
        print(f"  Updated secret: {name}")
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(Name=name, SecretString=payload)
        print(f"  Created secret: {name}")
    return name

def delete_secret(sm, name: str):
    try:
        sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        print(f"  Deleted secret: {name}")
    except sm.exceptions.ResourceNotFoundException:
        pass

# ── CloudWatch log group ───────────────────────────────────────────────────
def ensure_log_group(logs, name: str):
    try:
        logs.create_log_group(logGroupName=name)
        print(f"  Created log group: {name}")
    except logs.exceptions.ResourceAlreadyExistsException:
        pass

# ── ECS task definition ────────────────────────────────────────────────────
def register_task(ecs, cfg: dict, user: str, secret_arn: str) -> str:
    family = f"ptb-{user}"
    log_group = f"/ptb/{user}"

    task_def = {
        "family": family,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "1024",       # 1 vCPU
        "memory": "2048",    # 2 GB
        "executionRoleArn": cfg["ROLE_ARN"],
        "taskRoleArn":      cfg["ROLE_ARN"],
        "containerDefinitions": [{
            "name": "ptb",
            "image": f"{cfg['ECR_URI']}:latest",
            "portMappings": [{"containerPort": 8788, "protocol": "tcp"}],
            "essential": True,
            "secrets": [
                {"name": "API_KEY_USER1",      "valueFrom": f"{secret_arn}:API_KEY_USER1::"},
                {"name": "ANTHROPIC_API_KEY",   "valueFrom": f"{secret_arn}:ANTHROPIC_API_KEY::"},
                # TODO: API KEY — uncomment when polyctx switches to API keys:
                # {"name": "POLYAI_API_KEY",    "valueFrom": f"{secret_arn}:POLYAI_API_KEY::"},
            ],
            "environment": [
                {"name": "CLAUDE_BIN", "value": "claude"},
                {"name": "LAS_BIN",    "value": "las"},
                {"name": "PORT",       "value": "8788"},
                {"name": "JOBS_DIR",   "value": "/tmp/ptb_jobs"},
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group":         log_group,
                    "awslogs-region":        cfg["AWS_REGION"],
                    "awslogs-stream-prefix": "ptb",
                },
            },
        }],
    }

    resp = ecs.register_task_definition(**task_def)
    arn = resp["taskDefinition"]["taskDefinitionArn"]
    print(f"  Task definition: {arn}")
    return arn

# ── Fargate task ───────────────────────────────────────────────────────────
def get_subnets(ec2, vpc_id: str) -> list:
    resp = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    return [s["SubnetId"] for s in resp["Subnets"]]

def launch_task(ecs, cfg: dict, user: str, task_def_arn: str) -> str:
    subnets = get_subnets(boto3.client("ec2", region_name=cfg["AWS_REGION"]), cfg["VPC_ID"])

    resp = ecs.run_task(
        cluster=cfg["ECS_CLUSTER"],
        taskDefinition=task_def_arn,
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnets[:2],
                "securityGroups": [cfg["SG_ID"]],
                "assignPublicIp": "ENABLED",
            }
        },
        tags=[
            {"key": "ptb-user",    "value": user},
            {"key": "application", "value": "prompt-to-build"},
        ],
    )

    failures = resp.get("failures", [])
    if failures:
        sys.exit(f"❌ Task launch failed: {failures}")

    task_arn = resp["tasks"][0]["taskArn"]
    print(f"  Task launched: {task_arn}")
    return task_arn

def wait_for_task(ecs, cfg: dict, task_arn: str, timeout: int = 180) -> str:
    """Wait for task to be RUNNING and return its public IP."""
    print("  Waiting for task to start", end="", flush=True)
    ec2 = boto3.client("ec2", region_name=cfg["AWS_REGION"])
    start = time.time()

    while time.time() - start < timeout:
        resp = ecs.describe_tasks(cluster=cfg["ECS_CLUSTER"], tasks=[task_arn])
        task = resp["tasks"][0]
        status = task["lastStatus"]

        if status == "RUNNING":
            print(" ✓")
            # Get public IP from ENI
            for attachment in task.get("attachments", []):
                for detail in attachment.get("details", []):
                    if detail["name"] == "networkInterfaceId":
                        eni_id = detail["value"]
                        eni = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
                        ip = eni["NetworkInterfaces"][0].get("Association", {}).get("PublicIp")
                        return ip
        elif status in ("STOPPED", "DEPROVISIONING"):
            print()
            reason = task.get("stoppedReason", "unknown")
            sys.exit(f"❌ Task stopped unexpectedly: {reason}")

        print(".", end="", flush=True)
        time.sleep(5)

    print()
    sys.exit("❌ Timed out waiting for task to start.")

def stop_tasks_for_user(ecs, cfg: dict, user: str):
    resp = ecs.list_tasks(
        cluster=cfg["ECS_CLUSTER"],
        family=f"ptb-{user}",
    )
    for task_arn in resp.get("taskArns", []):
        ecs.stop_task(cluster=cfg["ECS_CLUSTER"], task=task_arn)
        print(f"  Stopped task: {task_arn}")

# ── State file (track running tasks per user) ──────────────────────────────
STATE_FILE = Path(__file__).parent / ".user_state.json"

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Main ───────────────────────────────────────────────────────────────────
def provision(args):
    cfg = load_config()
    c = clients(cfg["AWS_REGION"])

    user = args.user
    print(f"\n🚀 Provisioning user: {user}\n")

    # Generate or use provided API key
    api_key = args.api_key or f"ptb-{user}-{secrets.token_hex(16)}"

    # Store secrets
    print("▶ Writing secrets to AWS Secrets Manager...")
    secret_name = f"ptb/{user}"
    secret_value = {
        "API_KEY_USER1":    api_key,
        "ANTHROPIC_API_KEY": args.anthropic_key,
        # TODO: API KEY — add when available:
        # "POLYAI_API_KEY": args.polyai_key,
    }
    if args.polyai_key:
        secret_value["POLYAI_API_KEY"] = args.polyai_key

    secret_resp = c["sm"].describe_secret(SecretId=secret_name) if _secret_exists(c["sm"], secret_name) else None
    put_secret(c["sm"], secret_name, secret_value)
    secret_arn = c["sm"].describe_secret(SecretId=secret_name)["ARN"]

    # Log group
    print("\n▶ Ensuring CloudWatch log group...")
    ensure_log_group(c["logs"], f"/ptb/{user}")

    # Task definition
    print("\n▶ Registering ECS task definition...")
    task_def_arn = register_task(c["ecs"], cfg, user, secret_arn)

    # Stop any existing task for this user
    print("\n▶ Stopping any existing tasks...")
    stop_tasks_for_user(c["ecs"], cfg, user)
    time.sleep(3)

    # Launch new task
    print("\n▶ Launching Fargate task...")
    task_arn = launch_task(c["ecs"], cfg, user, task_def_arn)

    # Wait and get IP
    print("\n▶ Waiting for container to be ready...")
    public_ip = wait_for_task(c["ecs"], cfg, task_arn)

    # Save state
    state = load_state()
    state[user] = {
        "task_arn": task_arn,
        "task_def_arn": task_def_arn,
        "secret_name": secret_name,
        "public_ip": public_ip,
        "api_key": api_key,
        "url": f"http://{public_ip}:8788",
    }
    save_state(state)

    print(f"""
✅ User '{user}' is ready!

  URL:     http://{public_ip}:8788
  API key: {api_key}

  Web UI:  http://{public_ip}:8788
  Health:  http://{public_ip}:8788/health

Share the URL and API key with {user}.
""")

def destroy(args):
    cfg = load_config()
    c = clients(cfg["AWS_REGION"])
    user = args.user

    print(f"\n🗑  Destroying user: {user}\n")

    print("▶ Stopping tasks...")
    stop_tasks_for_user(c["ecs"], cfg, user)

    print("▶ Deleting secrets...")
    delete_secret(c["sm"], f"ptb/{user}")

    state = load_state()
    state.pop(user, None)
    save_state(state)

    print(f"\n✅ User '{user}' deprovisioned.")

def list_users(args):
    state = load_state()
    if not state:
        print("No users provisioned yet.")
        return
    print(f"\n{'USER':<12} {'URL':<35} {'API KEY'}")
    print("─" * 80)
    for user, info in state.items():
        print(f"{user:<12} {info['url']:<35} {info['api_key']}")
    print()

def _secret_exists(sm, name: str) -> bool:
    try:
        sm.describe_secret(SecretId=name)
        return True
    except sm.exceptions.ResourceNotFoundException:
        return False

# ── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Provision Prompt to Build users on AWS Fargate")
    sub = parser.add_subparsers(dest="command")

    # provision
    p = sub.add_parser("provision", aliases=["up"], help="Spin up a user container")
    p.add_argument("--user",          required=True, help="Username (e.g. alice)")
    p.add_argument("--anthropic-key", required=True, help="Anthropic API key (sk-ant-...)")
    p.add_argument("--polyai-key",    default=None,  help="PolyAI API key (available next week)")
    p.add_argument("--api-key",       default=None,  help="PTB API key for the user (auto-generated if omitted)")
    p.set_defaults(func=provision)

    # destroy
    d = sub.add_parser("destroy", aliases=["down"], help="Tear down a user container")
    d.add_argument("--user", required=True)
    d.set_defaults(func=destroy)

    # list
    l = sub.add_parser("list", aliases=["ls"], help="List provisioned users")
    l.set_defaults(func=list_users)

    # Support old-style: provision_user.py --user alice --anthropic-key ...
    if len(sys.argv) > 1 and sys.argv[1].startswith("--"):
        sys.argv.insert(1, "provision")

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    args.func(args)
