# SQL Test Generator

Generates plain-English data quality test cases from a SQL query, using a three-pass
reasoning pipeline (Understand → Generate → Self-Critique) on AWS Bedrock.

---

## Beginner guide: run this on AWS from a fresh machine

Everything below assumes you are starting on a brand-new laptop/EC2 box with nothing
installed except a terminal.

### Step 0 — What you need before starting

- An AWS account where **Amazon Bedrock is enabled**
- Permission to create IAM roles and Lambda functions
- The clone URL of this repository (copy it from your Git host's **Code** button)

### Step 1 — Install the tools

```bash
# macOS
brew install git awscli python@3.11

# Ubuntu / Amazon Linux
sudo apt update && sudo apt install -y git python3 python3-pip unzip
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip && sudo ./aws/install
```

Check they work:

```bash
git --version
python3 --version   # need 3.10 or newer
aws --version
```

### Step 2 — Get the code from GitHub

```bash
git clone <YOUR_REPO_URL>
cd <REPO_DIRECTORY>
```

### Step 3 — Connect the machine to your AWS account

```bash
aws configure
# AWS Access Key ID:     <from IAM > Users > Security credentials>
# AWS Secret Access Key: <same place>
# Default region name:   us-east-1
# Default output format: json
```

Verify:

```bash
aws sts get-caller-identity
```

If that prints your account number, the machine is connected.

### Step 4 — Turn on model access in Bedrock (one-time, per account)

1. Open the AWS Console → **Amazon Bedrock** → **Model access** (left menu)
2. Click **Modify model access**
3. Tick **Amazon Nova Pro** (and Nova Lite / Nova Micro if you want cheaper options)
4. Submit — access is usually granted in under a minute

Confirm from the terminal:

```bash
aws bedrock list-foundation-models --region us-east-1 \
  --query "modelSummaries[?contains(modelId,'nova')].modelId" --output table
```

### Step 5 — Test it locally first (optional but recommended)

This runs the same code, calling Bedrock over the network:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python3 -c "
import json, agent
payload = json.load(open('examples/sample_payload.json'))
print(json.dumps(agent.invoke(payload), indent=2))
"
```

You should see a `query_summary` and a numbered list of test cases. If this works,
the AWS side is configured correctly.

### Step 6 — Create the IAM role Lambda will use

```bash
cat > trust-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name sql-test-generator-role \
  --assume-role-policy-document file://trust-policy.json

# Lets Lambda write logs to CloudWatch
aws iam attach-role-policy \
  --role-name sql-test-generator-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Lets Lambda call Bedrock models
cat > bedrock-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel"],
    "Resource": "*"
  }]
}
EOF

aws iam put-role-policy \
  --role-name sql-test-generator-role \
  --policy-name bedrock-invoke \
  --policy-document file://bedrock-policy.json
```

### Step 7 — Package and deploy the Lambda

`boto3` is already inside the Lambda runtime, so the zip only needs your `.py` files.

```bash
zip deploy.zip agent.py llm_client.py prompts.py

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws lambda create-function \
  --function-name sql-test-generator \
  --runtime python3.11 \
  --role arn:aws:iam::${ACCOUNT_ID}:role/sql-test-generator-role \
  --handler agent.lambda_handler \
  --zip-file fileb://deploy.zip \
  --timeout 300 \
  --memory-size 512 \
  --region us-east-1
```

> Three passes × one model call each takes time — the 300 second timeout matters.

### Step 8 — Run it on AWS

```bash
aws lambda invoke \
  --function-name sql-test-generator \
  --payload fileb://examples/sample_payload.json \
  --cli-binary-format raw-in-base64-out \
  --region us-east-1 \
  response.json

cat response.json
```

Watch the logs live in another terminal:

```bash
aws logs tail /aws/lambda/sql-test-generator --follow
```

### Step 9 — Update after a code change

```bash
git pull
zip deploy.zip agent.py llm_client.py prompts.py
aws lambda update-function-code \
  --function-name sql-test-generator \
  --zip-file fileb://deploy.zip \
  --region us-east-1
```

### Step 10 (optional) — Expose it as an HTTP endpoint

```bash
aws lambda create-function-url-config \
  --function-name sql-test-generator \
  --auth-type AWS_IAM \
  --region us-east-1
```

`lambda_handler` already detects an HTTP-style event and returns a proper
`statusCode` / `body` response, so no code change is needed.

---

## Choosing the model

The model is read from the `BEDROCK_MODEL_ID` environment variable, defaulting to
`us.amazon.nova-pro-v1:0` (see [llm_client.py:24](llm_client.py#L24)).

| Model ID | Speed | Cost | Quality |
|---|---|---|---|
| `us.amazon.nova-micro-v1:0` | fastest | lowest | basic |
| `us.amazon.nova-lite-v1:0` | fast | low | good |
| `us.amazon.nova-pro-v1:0` | moderate | medium | best of Nova |
| `us.anthropic.claude-3-5-sonnet-20241022-v2:0` | moderate | highest | highest |

Switch it without touching the code:

```bash
aws lambda update-function-configuration \
  --function-name sql-test-generator \
  --environment "Variables={BEDROCK_MODEL_ID=us.amazon.nova-lite-v1:0}" \
  --region us-east-1
```

---

## Common errors

| Error | Meaning | Fix |
|---|---|---|
| `AccessDeniedException ... bedrock:InvokeModel` | Role can't call Bedrock | Redo Step 6 |
| `You don't have access to the model` | Model access not requested | Redo Step 4 |
| `ValidationException: on-demand throughput isn't supported` | Model needs an inference profile | Use the `us.` prefixed model ID |
| `Task timed out after 3.00 seconds` | Timeout too low | `--timeout 300` |
| `Unable to locate credentials` (local) | Machine not configured | Redo Step 3 |
