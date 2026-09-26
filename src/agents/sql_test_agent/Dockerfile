# syntax=docker/dockerfile:1
#
# AgentCore Runtime only accepts linux/arm64 images, and it rejects anything else
# at CreateAgentRuntime -- long after the build has been paid for. The platform
# is therefore pinned in the FROM line rather than left to the builder's host.
#
# Build from the REPOSITORY ROOT so the shared library is inside the context:
#
#   docker build --platform linux/arm64 \
#     -f src/agents/sql_test_agent/Dockerfile \
#     -t <account>.dkr.ecr.eu-central-1.amazonaws.com/tmnl-tina-sql-test-agent:latest .
#
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

# Unbuffered so CloudWatch sees a log line when it happens and not when the
# buffer flushes, which for a 5-minute Bedrock call is far too late to help.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: they change far less often than the agent, so this layer
# survives most rebuilds.
COPY src/agents/sql_test_agent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# tina_agent_base is imported as a top-level package, so it is installed beside
# the agent instead of under src/. sql_test_assistant.py also adds the checkout
# path when it finds one, which is how the same file runs from a developer shell.
COPY src/agents/agents_base/tina_agent_base ./tina_agent_base

# agent.yaml must sit next to the module: config.load_settings() looks for it
# there, and the prompts and limits live in it.
COPY src/agents/sql_test_agent/sql_test_assistant.py ./sql_test_assistant.py
# Imported by sql_test_assistant.py as a top-level module, so it sits beside it.
COPY src/agents/sql_test_agent/session_memory.py ./session_memory.py
COPY src/agents/sql_test_agent/agent.yaml ./agent.yaml

# Non-root. The runtime never needs root, and a prompt-injected model response
# should not be able to rewrite the agent that produced it.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agent \
    && chown -R agent:agent /app
USER agent

# The bedrock-agentcore SDK serves POST /invocations and GET /ping here. The
# runtime health-checks /ping itself, so the image adds no HEALTHCHECK of its own.
EXPOSE 8080

CMD ["python", "sql_test_assistant.py"]
