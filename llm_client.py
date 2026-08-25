"""Groq LLM client for invoking the language model."""

import os
from groq import Groq


class LLMClient:
    """Wrapper for Groq API calls."""

    def __init__(self, api_key: str | None = None, model: str = "openai/gpt-oss-120b"):
        """Initialize Groq client.

        Args:
            api_key: Groq API key. If not provided, will read from GROQ_API_KEY env var.
            model: Model ID to use. Defaults to mixtral-8x7b-32768.
        """
        if not api_key:
            api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY not found in environment variables")

        self.client = Groq(api_key=api_key)
        self.model = model

    def invoke(self, prompt: str) -> str:
        """Invoke the model with a prompt and return the text response.

        Args:
            prompt: The prompt to send to the model.

        Returns:
            The text content of the model's response.

        Raises:
            ValueError: If the response is empty or malformed.
        """
        message = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2048,
        )

        if not message.choices or not message.choices[0].message.content:
            raise ValueError("Empty response from Groq API")

        return message.choices[0].message.content
