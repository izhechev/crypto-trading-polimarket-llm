import json
import logging
from typing import Optional, Dict, Any
import config
import warnings

logger = logging.getLogger("llm_client")

class LLMClient:
    def __init__(self):
        # Groq Setup
        self.groq_client = None
        if config.GROQ_API_KEY:
            try:
                from groq import Groq
                self.groq_client = Groq(api_key=config.GROQ_API_KEY)
            except Exception as e:
                logger.error(f"Groq init failed: {e}")

        # Gemini Setup
        self.gemini_model = None
        if config.GEMINI_API_KEY:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    import google.generativeai as genai
                genai.configure(api_key=config.GEMINI_API_KEY)
                self.gemini_model = genai.GenerativeModel('gemini-3.1-flash-lite-preview')
            except Exception as e:
                logger.error(f"Gemini init failed: {e}")

    def call(self, prompt: str, system_prompt: str = "You are a helpful assistant.") -> Dict[str, Any]:
        # Try Groq first
        if self.groq_client:
            try:
                response = self.groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                )
                return json.loads(response.choices[0].message.content)
            except Exception as _groq_e:
                logger.debug(f"Groq failed: {_groq_e}")

        # Fallback to Gemini
        if self.gemini_model:
            try:
                full_prompt = f"{system_prompt}\n\n{prompt}"
                response = self.gemini_model.generate_content(full_prompt)
                text = response.text.replace("```json", "").replace("```", "").strip()
                return json.loads(text)
            except Exception as e:
                logger.error(f"Gemini analysis failed: {e}")

        return {"error": "All LLM providers failed"}
