"""AWS Bedrock LLM client for invoking language models."""
 
import os
from typing import Optional
 
import boto3
 
 
class LLMClient:
    """Wrapper for AWS Bedrock API calls using the Converse API."""
 
    def __init__(
        self,
        region: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """Initialize the Amazon Bedrock client."""
        self.region = (
            region
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "eu-central-1"
        )
 
        self.model = (
            model
            or os.environ.get("BEDROCK_MODEL_ID")
            or "arn:aws:bedrock:eu-central-1:492098925493:application-inference-profile/4olq2nnbqrk1"
        )
 
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=self.region,
        )
 
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
                inferenceConfig={
                    "maxTokens": 4096,
                },
                additionalModelRequestFields={
                    "thinking": {"type": "disabled"},
                },
            )
        except Exception as exc:
            raise ValueError(
                f"Bedrock invocation failed in region '{self.region}' using model "
                f"'{self.model}': {exc}"
            ) from exc
 
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
            ] if "content" in locals() and isinstance(content, list) else []
            raise ValueError(
                "Bedrock returned no text content (stopReason='{}', "
                "contentTypes={}). Disable reasoning or increase maxTokens."
                .format(stop_reason, content_types)
            ) from exc
 
        if not text or not text.strip():
            raise ValueError("Bedrock returned an empty response")
 
        return text
 
