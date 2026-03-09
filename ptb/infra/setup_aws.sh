#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# One-time AWS setup for Prompt to Build
# Run this ONCE to create the shared infrastructure.
# After this, use provision_user.py to spin up individual users.
#
# Requirements: aws CLI configured, Docker installed
# Usage: bash infra/setup_aws.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────
AWS_REGION="${AWS_REGION:-eu-west-2}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="prompt-to-build"
ECS_CLUSTER="prompt-to-build"
LAS_REPO_URL="${LAS_REPO_URL:-}"  # set this to your internal las repo URL

echo "Account: $AWS_ACCOUNT_ID  Region: $AWS_REGION"
echo ""

# ── ECR repository ─────────────────────────────────────────────────────────
echo "▶ Creating ECR repository..."
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null 2>&1 || \
  aws ecr create-repository \
    --repository-name "$ECR_REPO" \
    --region "$AWS_REGION" \
    --image-scanning-configuration scanOnPush=true \
    --query "repository.repositoryUri" --output text

ECR_URI="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO"
echo "  ECR: $ECR_URI"

# ── Build and push image ────────────────────────────────────────────────────
echo ""
echo "▶ Building Docker image..."
docker build \
  --build-arg LAS_REPO_URL="$LAS_REPO_URL" \
  -t "$ECR_REPO:latest" \
  -f Dockerfile .

echo "▶ Pushing to ECR..."
aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "$ECR_URI"

docker tag "$ECR_REPO:latest" "$ECR_URI:latest"
docker push "$ECR_URI:latest"
echo "  Pushed: $ECR_URI:latest"

# ── ECS cluster ────────────────────────────────────────────────────────────
echo ""
echo "▶ Creating ECS cluster..."
aws ecs describe-clusters --clusters "$ECS_CLUSTER" --region "$AWS_REGION" \
  --query "clusters[0].status" --output text 2>/dev/null | grep -q ACTIVE || \
  aws ecs create-cluster \
    --cluster-name "$ECS_CLUSTER" \
    --region "$AWS_REGION" \
    --capacity-providers FARGATE \
    --query "cluster.clusterName" --output text
echo "  Cluster: $ECS_CLUSTER"

# ── IAM role for ECS tasks (to read Secrets Manager) ──────────────────────
echo ""
echo "▶ Creating ECS task execution role..."
ROLE_NAME="ptb-task-execution-role"

aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1 || \
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }' >/dev/null

aws iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy \
  2>/dev/null || true

# Allow tasks to read Secrets Manager
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name ptb-secrets-policy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": "arn:aws:secretsmanager:*:*:secret:ptb/*"
    }]
  }' 2>/dev/null || true

ROLE_ARN="arn:aws:iam::$AWS_ACCOUNT_ID:role/$ROLE_NAME"
echo "  Role: $ROLE_ARN"

# ── Security group ─────────────────────────────────────────────────────────
echo ""
echo "▶ Creating security group..."
VPC_ID=$(aws ec2 describe-vpcs \
  --filters "Name=isDefault,Values=true" \
  --query "Vpcs[0].VpcId" --output text \
  --region "$AWS_REGION")

SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=ptb-sg" "Name=vpc-id,Values=$VPC_ID" \
  --query "SecurityGroups[0].GroupId" --output text \
  --region "$AWS_REGION" 2>/dev/null || echo "None")

if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
  SG_ID=$(aws ec2 create-security-group \
    --group-name "ptb-sg" \
    --description "Prompt to Build — allow port 8788 inbound" \
    --vpc-id "$VPC_ID" \
    --region "$AWS_REGION" \
    --query "GroupId" --output text)

  aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --protocol tcp --port 8788 --cidr 0.0.0.0/0 \
    --region "$AWS_REGION" >/dev/null
fi
echo "  Security group: $SG_ID"

# ── Write config for provision_user.py ────────────────────────────────────
echo ""
cat > infra/.aws_config << EOF
AWS_REGION=$AWS_REGION
AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID
ECR_URI=$ECR_URI
ECS_CLUSTER=$ECS_CLUSTER
ROLE_ARN=$ROLE_ARN
VPC_ID=$VPC_ID
SG_ID=$SG_ID
EOF
echo "▶ Saved config to infra/.aws_config"

echo ""
echo "✅ AWS setup complete. Run next:"
echo "   python3 infra/provision_user.py --user user1 --anthropic-key sk-ant-... --polyai-key <key>"
