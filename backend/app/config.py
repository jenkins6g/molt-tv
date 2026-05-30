import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(override=True)


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    openrouter_base_url: str

    nemotron_llm_url: str
    nemotron_llm_api_key: str
    nemotron_llm_model: str
    nemotron_enable_thinking: bool
    nvidia_asr_url: str

    gradium_api_key: str
    gradium_voice_id: str

    openai_api_key: str

    twilio_account_sid: str
    twilio_auth_token: str

    cekura_api_key: str
    cekura_base_url: str
    cekura_agent_id: str

    sqlite_path: str
    faiss_path: str
    phase: int


def load() -> Settings:
    g = os.environ.get
    return Settings(
        openrouter_api_key=g("OPENROUTER_API_KEY", ""),
        openrouter_base_url=g("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        nemotron_llm_url=g("NEMOTRON_LLM_URL", ""),
        nemotron_llm_api_key=g("NEMOTRON_LLM_API_KEY", "EMPTY"),
        nemotron_llm_model=g("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
        nemotron_enable_thinking=g("NEMOTRON_ENABLE_THINKING", "false").lower() == "true",
        nvidia_asr_url=g("NVIDIA_ASR_URL", ""),
        gradium_api_key=g("GRADIUM_API_KEY", ""),
        gradium_voice_id=g("GRADIUM_VOICE_ID", ""),
        openai_api_key=g("OPENAI_API_KEY", ""),
        twilio_account_sid=g("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=g("TWILIO_AUTH_TOKEN", ""),
        cekura_api_key=g("CEKURA_API_KEY", ""),
        cekura_base_url=g("CEKURA_BASE_URL", "https://api.cekura.ai"),
        cekura_agent_id=g("CEKURA_AGENT_ID", ""),
        sqlite_path=g("SQLITE_PATH", "./data/civicpilot.db"),
        faiss_path=g("FAISS_PATH", "./data/failures.faiss"),
        phase=int(g("PHASE", "1")),
    )


settings = load()
