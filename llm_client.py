"""AWS Bedrock LLM client for invoking language models."""

import os
import boto3


class LLMClient:
    """Wrapper for AWS Bedrock API calls (Converse API)."""

    def __init__(self, region: str | None = None, model: str | None = None):
        """Initialize Bedrock client.

        Args:
            region: AWS region for Bedrock. Falls back to $AWS_REGION, then us-east-1.
            model: Model ID to use. Falls back to $BEDROCK_MODEL_ID, then
                   us.amazon.nova-pro-v1:0. Other options:
                   - us.amazon.nova-micro-v1:0 (fastest, cheapest)
                   - us.amazon.nova-lite-v1:0 (balanced)
                   - us.amazon.nova-pro-v1:0 (most capable)
                   - us.anthropic.claude-3-5-sonnet-20241022-v2:0 (Claude Sonnet)
        """
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.model = model or os.environ.get(
            "BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0"
        )
        self.client = boto3.client("bedrock-runtime", region_name=self.region)

    def invoke(self, prompt: str) -> str:
        """Invoke the Bedrock model with a prompt and return the text response.

        Args:
            prompt: The prompt to send to the model.

        Returns:
            The text content of the model's response.

        Raises:
            ValueError: If the response is empty or malformed.
        """
        try:
            response = self.client.converse(
                modelId=self.model,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"temperature": 0.3, "maxTokens": 2048},
            )
        except Exception as e:
            raise ValueError(f"Bedrock invocation failed: {str(e)}") from e

        try:
            return response["output"]["message"]["content"][0]["text"]
        except (KeyError, IndexError) as e:
            raise ValueError(f"Unexpected Bedrock response shape: {response}") from e
