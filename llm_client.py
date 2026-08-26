"""AWS Bedrock LLM client for invoking language models."""

import os
from typing import Any, Optional

import boto3
from botocore.config import Config


class LLMClient:
    """Wrapper for AWS Bedrock API calls using the Converse API."""

    def __init__(
        self,
        region: Optional[str] = None,
        model: Optional[str] = None,
        settings: Optional[dict] = None,
    ):
        """Initialize the Amazon Bedrock client.

        Args:
            region: Overrides the environment and the `settings` region.
            model: Overrides the environment and the `settings` model_id.
            settings: A `bedrock` block from agent.yaml supplying max_tokens,
                temperature, thinking, and retries.
        """
        self.settings: dict[str, Any] = settings or {}

        self.region = (
            region
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or self.settings.get("region")
            or "eu-central-1"
        )

        # Accepts a model ID, an inference-profile ID, or a profile ARN.
        self.model = (
            model
            or os.environ.get("BEDROCK_MODEL_ID")
            or self.settings.get("model_id")
        )

        if not self.model:
            raise ValueError(
                "No Bedrock model configured. Set model_id in agent.yaml, or the "
                "BEDROCK_MODEL_ID environment variable, for example:\n"
                "  export BEDROCK_MODEL_ID=eu.amazon.nova-pro-v1:0"
            )

        retries = self.settings.get("retries") or {}

        self.client = boto3.client(
            "bedrock-runtime",
            region_name=self.region,
            config=Config(
                retries={
                    "max_attempts": retries.get("max_attempts", 5),
                    "mode": retries.get("mode", "adaptive"),
                }
            ),
        )

    def _inference_config(self) -> dict:
        """Build the Converse inferenceConfig from agent.yaml settings."""
        config: dict[str, Any] = {"maxTokens": self.settings.get("max_tokens", 4096)}

        temperature = self.settings.get("temperature")
        if temperature is not None:
            config["temperature"] = temperature

        return config

    def _additional_fields(self) -> dict:
        """Reasoning tokens can crowd out the text response; allow disabling."""
        if self.settings.get("thinking", "disabled") == "disabled":
            return {"thinking": {"type": "disabled"}}
        return {}

    def invoke(self, prompt: str) -> str:
        """Invoke the Bedrock model and return its text response."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt must be a non-empty string")

        try:
            response = self.client.converse(
                modelId=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": prompt}],
                    }
                ],
                inferenceConfig=self._inference_config(),
                additionalModelRequestFields=self._additional_fields(),
            )
        except Exception as exc:
            raise ValueError(
                f"Bedrock invocation failed in region '{self.region}' using model "
                f"'{self.model}': {exc}"
            ) from exc

        content = None

        try:
            content = response["output"]["message"]["content"]
            text = next(
                item["text"]
                for item in content
                if isinstance(item, dict) and item.get("text")
            )
        except (KeyError, StopIteration, TypeError) as exc:
            stop_reason = response.get("stopReason", "unknown")
            content_types = [
                next(iter(item), "unknown")
                for item in content
                if isinstance(item, dict)
            ] if isinstance(content, list) else []
            raise ValueError(
                "Bedrock returned no text content (stopReason='{}', "
                "contentTypes={}). Disable reasoning or increase max_tokens."
                .format(stop_reason, content_types)
            ) from exc

        if not text or not text.strip():
            raise ValueError("Bedrock returned an empty response")

        return text
