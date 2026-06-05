# SPDX-License-Identifier: MIT
# LLM Client - Dual path: Company CATL API + Local fallback model
# Auto-switch: primary fails → automatic fallback to local model

import os
import logging
import time
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Company API Client (CATL internal LLM - Kimi K2.5 or similar)
# ------------------------------------------------------------------------------

class CompanyAPIClient:
    """Calls company internal LLM service (CATL Kimi K2.5 or similar)"""

    def __init__(self):
        self.base_url = os.getenv("COMPANY_LLM_API_BASE", "https://llm.catlbattery.com/v1")
        self.api_key = os.getenv("COMPANY_LLM_API_KEY", os.getenv("LLM_API_KEY", ""))
        self.model = os.getenv("COMPANY_LLM_MODEL", "kimi-k2.5")
        self.timeout = int(os.getenv("COMPANY_LLM_TIMEOUT", "30"))

    def generate(self, prompt: str, system: str = "", **kwargs) -> str:
        """Call company LLM API with timeout"""
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system} if system else None,
                {"role": "user", "content": prompt}
            ].filter(None),
            "temperature": kwargs.get("temperature", 0.0),
            "max_tokens": kwargs.get("max_tokens", 4096)
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning(f"Company API error: {e}")
            raise

    def is_available(self) -> bool:
        """Check if company API is reachable"""
        import httpx
        try:
            with httpx.Client(timeout=5) as client:
                r = client.get(f"{self.base_url.rstrip('/v1')}/models",
                              headers={"Authorization": f"Bearer {self.api_key}"})
                return r.status_code == 200
        except Exception:
            return False


# ------------------------------------------------------------------------------
# Local Model Client (Qwen/DeepSeek via vLLM or Ollama)
# ------------------------------------------------------------------------------

class LocalModelClient:
    """Calls locally deployed model via vLLM/Ollama API"""

    def __init__(self):
        self.base_url = os.getenv("LOCAL_LLM_API_BASE", "http://localhost:8000/v1")
        self.model = os.getenv("LOCAL_LLM_MODEL", "qwen2.5-7b-instruct")
        self.timeout = int(os.getenv("LOCAL_LLM_TIMEOUT", "120"))

    def generate(self, prompt: str, system: str = "", **kwargs) -> str:
        """Call local LLM API"""
        import httpx

        headers = {"Content-Type": "application/json"}

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system} if system else None,
                {"role": "user", "content": prompt}
            ].filter(None),
            "temperature": kwargs.get("temperature", 0.0),
            "max_tokens": kwargs.get("max_tokens", 4096)
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning(f"Local model error: {e}")
            raise

    def is_available(self) -> bool:
        """Check if local model is running"""
        import httpx
        try:
            with httpx.Client(timeout=5) as client:
                r = client.get(f"{self.base_url.rstrip('/v1')}/models")
                return r.status_code == 200
        except Exception:
            return False


# ------------------------------------------------------------------------------
# Dual-path LLM Client (main entry point for all services)
# ------------------------------------------------------------------------------

class LLMClient:
    """
    Unified LLM client with automatic failover.
    Tries company API first; on failure/timeout → falls back to local model.
    """

    def __init__(self):
        self.primary = CompanyAPIClient()
        self.fallback = LocalModelClient()
        self._fallback_tried = False

    def generate(
        self,
        prompt: str,
        system: str = "",
        use_cache: bool = True,
        **kwargs
    ) -> str:
        """
        Generate with dual-path fallback.

        Args:
            prompt: User prompt
            system: System prompt
            use_cache: Whether to cache results in Redis
            **kwargs: passed to underlying API (temperature, max_tokens, etc.)

        Returns:
            Generated text string
        """
        # Try primary company API
        try:
            logger.info("Attempting company LLM API (CATL)...")
            result = self.primary.generate(prompt, system=system, **kwargs)
            logger.info("Company LLM API succeeded")
            return result
        except Exception as e:
            logger.warning(f"Company LLM API failed: {e}")

        # Fall back to local model
        try:
            logger.info("Falling back to local model...")
            result = self.fallback.generate(prompt, system=system, **kwargs)
            logger.info("Local model succeeded")
            return result
        except Exception as e:
            logger.error(f"Both company API and local model failed: {e}")
            return f"[LLM Error: both primary and fallback failed. Last error: {e}]"

    def generate_with_few_shot(
        self,
        prompt: str,
        examples: List[Dict[str, str]],
        system: str = "",
        **kwargs
    ) -> str:
        """
        Generate with few-shot examples injected before the prompt.
        Examples format: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
        """
        full_prompt = self._build_few_shot_prompt(prompt, examples)
        return self.generate(full_prompt, system=system, **kwargs)

    def _build_few_shot_prompt(self, prompt: str, examples: List[Dict[str, str]]) -> str:
        parts = []
        for ex in examples:
            parts.append(f"User: {ex.get('input', '')}")
            parts.append(f"Assistant: {ex.get('output', '')}")
        parts.append(f"User: {prompt}")
        parts.append("Assistant:")
        return "\n\n".join(parts)

    def status(self) -> Dict[str, Any]:
        """Return health status of both paths"""
        return {
            "primary_available": self.primary.is_available(),
            "primary_model": self.primary.model,
            "fallback_available": self.fallback.is_available(),
            "fallback_model": self.fallback.model
        }


# ------------------------------------------------------------------------------
# Entity/Relation extraction helpers (used by graphify)
# ------------------------------------------------------------------------------

def build_entity_extraction_prompt(text: str, chunk_size: int = 512) -> str:
    """Build a prompt for entity extraction from text chunks"""
    return f"""You are an expert at extracting structured knowledge from technical documents.

Extract ALL entities (named concepts, objects, processes, tools, locations, people) from the text below.
For each entity, provide its name and type.

Return a JSON list (no markdown, no code blocks):
[
  {{"name": "entity name", "type": "PROCESS|MACHINE|PERSON|LOCATION|MATERIAL|QUALITY|ACTION|CONCEPT"}},
  ...
]

Text:
{text[:chunk_size]}

Rules:
- Extract entities with capital letters, technical terms, proper nouns
- Type must be one of: PROCESS, MACHINE, PERSON, LOCATION, MATERIAL, QUALITY, ACTION, CONCEPT
- If no entities found, return []
- Be thorough - miss an entity = quality issue"""


def build_relation_extraction_prompt(text: str, chunk_size: int = 512) -> str:
    """Build a prompt for relation extraction from text chunks"""
    return f"""You are an expert at extracting structured relationships between entities from technical documents.

Extract ALL relationships (who does what to whom, what causes what, what belongs to what) from the text below.

Return a JSON list (no markdown, no code blocks):
[
  {{"from": "entity A", "to": "entity B", "type": "USES|CONSISTS_OF|PRECEDES|FOLLOWS|CONTAINS|AFFECTS|PRODUCES"}},
  ...
]

Text:
{text[:chunk_size]}

Rules:
- Use exact entity names from the text
- Relation type must be one of: USES, CONSISTS_OF, PRECEDES, FOLLOWS, CONTAINS, AFFECTS, PRODUCES
- If no relations found, return []
- Direction matters: "A uses B" means from A to B"""


def parse_json_response(response: str) -> List[Dict[str, str]]:
    """Safely parse JSON from LLM response"""
    import json
    try:
        # Try direct parse
        return json.loads(response)
    except json.JSONDecodeError:
        # Try to extract from markdown code blocks
        import re
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
        # Try to find raw JSON array
        match = re.search(r'\[\s*\{[\s\S]*\}\s*\]', response)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return []


# Singleton instance (lazy initialization)
_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Get or create the singleton LLM client"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client