"""AWS Bedrock LLM client for invoking language models."""

import json
import boto3


class LLMClient:
    """Wrapper for AWS Bedrock API calls."""

    def __init__(self, region: str = "us-east-1", model: str = "amazon.nova-pro-v1:0"):
        """Initialize Bedrock client.

        Args:
            region: AWS region for Bedrock. Defaults to us-east-1.
            model: Model ID to use. Defaults to amazon.nova-pro-v1:0.
                   Other options:
                   - amazon.nova-micro-v1:0 (fastest, cheapest)
                   - amazon.nova-lite-v1:0 (balanced)
                   - amazon.nova-pro-v1:0 (most capable)
                   - anthropic.claude-3-5-sonnet-20241022-v2:0 (Claude Sonnet)
        """
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.model = model

    def invoke(self, prompt: str) -> str:
        """Invoke the Bedrock model with a prompt and return the text response.

        Args:
            prompt: The prompt to send to the model.

        Returns:
            The text content of the model's response.

        Raises:
            ValueError: If the response is empty or malformed.
        """
        request_body = {
            "schemaVersion": "1.0",
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "inferenceConfig": {
                "temperature": 0.3,
                "maxTokens": 2048,
            },
        }

        try:
            response = self.client.invoke_model(
                modelId=self.model,
                body=json.dumps(request_body),
            )

            response_body = json.loads(response["body"].read())

            if "content" not in response_body or not response_body["content"]:
                raise ValueError("Empty response from Bedrock API")

            return response_body["content"][0]["text"]

        except Exception as e:
            raise ValueError(f"Bedrock invocation failed: {str(e)}") from e
