"""Fournisseurs de LLM : detection des modeles disponibles, construction, explication des erreurs."""
import re

import httpx
from agno.models.google import Gemini
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.models.openrouter import OpenRouter

PROVIDERS = {
    "Gemini": {"caption": "Gratuit (cle Google)", "secret": "GOOGLE_API_KEY", "key_label": "Google Gemini API Key",
               "help": "https://aistudio.google.com/apikey"},
    "OpenRouter": {"caption": "Modeles gratuits (cle sk-or-...)", "secret": "OPENROUTER_API_KEY",
                   "key_label": "OpenRouter API Key", "help": "https://openrouter.ai/keys"},
    "OpenAI": {"caption": "Payant (cle sk-...)", "secret": "OPENAI_API_KEY", "key_label": "OpenAI API Key",
               "help": "https://platform.openai.com/api-keys"},
    "Ollama": {"caption": "Local, gratuit (GPU conseille)", "secret": None},
}
EXCLUDED = ("image", "tts", "audio", "live", "embedding", "vision", "robotics", "computer-use")
# Editeurs connus, essayes en premier sur OpenRouter (plus fiables pour piloter des agents)
TRUSTED_OPENROUTER = ("openai/gpt-oss", "qwen/", "meta-llama/", "deepseek/", "google/gemma", "mistralai/", "z-ai/", "moonshotai/")


def version_of(model_id: str) -> tuple:
    numbers = [int(n) for n in re.findall(r"\d+", model_id)[:2]]
    return tuple(numbers) if numbers else (0,)


def gemini_sort_key(model_id: str):
    """Stable avant preview, lite d'abord (plus rapide et moins sature), version recente d'abord."""
    is_preview = "preview" in model_id or "exp" in model_id
    return (is_preview, "lite" not in model_id, tuple(-n for n in version_of(model_id)))


def keep_latest_generation(ids: list[str]) -> list[str]:
    """Google liste encore d'anciens modeles refuses aux nouveaux comptes (404) : on garde la derniere generation."""
    if not ids:
        return ids
    latest = max(version_of(m)[0] for m in ids)
    return [m for m in ids if version_of(m)[0] == latest]


def list_gemini_models(api_key: str) -> list[str]:
    from google import genai

    ids = []
    for m in genai.Client(api_key=api_key).models.list():
        model_id = m.name.replace("models/", "")
        actions = getattr(m, "supported_actions", None) or []
        if "flash" not in model_id or any(word in model_id for word in EXCLUDED):
            continue
        if actions and "generateContent" not in actions:
            continue
        ids.append(model_id)
    return sorted(keep_latest_generation(sorted(set(ids))), key=gemini_sort_key)


def openrouter_rank(model_id: str) -> int:
    for i, prefix in enumerate(TRUSTED_OPENROUTER):
        if model_id.startswith(prefix):
            return i
    return len(TRUSTED_OPENROUTER)


def list_openrouter_free_models(api_key: str) -> list[str]:
    r = httpx.get("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
    r.raise_for_status()
    free = []
    for m in r.json().get("data", []):
        pricing = m.get("pricing", {})
        try:
            is_free = float(pricing.get("prompt", -1)) == 0 and float(pricing.get("completion", -1)) == 0
        except (TypeError, ValueError):
            is_free = False
        params = m.get("supported_parameters")
        if is_free and (params is None or "tools" in params):
            free.append((openrouter_rank(m["id"]), -(m.get("context_length") or 0), m["id"]))
    return [model_id for _, _, model_id in sorted(free)][:30]


def list_ollama_models(host: str) -> list[str]:
    import ollama

    return sorted(m.model for m in ollama.Client(host=host).list().models)


def list_models(provider: str, api_key: str, ollama_host: str) -> list[str]:
    if provider == "Gemini":
        return list_gemini_models(api_key)
    if provider == "OpenRouter":
        return list_openrouter_free_models(api_key)
    if provider == "OpenAI":
        return ["gpt-4o-mini", "gpt-4o"]
    return list_ollama_models(ollama_host)


def model_size_b(model_id: str) -> float:
    """Taille approximative lue dans le nom (ex. gpt-oss-20b -> 20). Inconnue -> tres grande."""
    sizes = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)b\b", model_id.lower())]
    return min(sizes) if sizes else 1e9


def default_roles(provider: str, available: list[str]) -> tuple[str, str]:
    """Routage par role : un modele qui raisonne bien pour le chef d'equipe (il planifie et redige),
    un modele rapide et economique pour les agents (ils appellent un outil et resument)."""
    if provider == "OpenAI":
        return ("gpt-4o" if "gpt-4o" in available else available[0]), ("gpt-4o-mini" if "gpt-4o-mini" in available else available[0])
    if provider == "Gemini":
        strong = [m for m in available if "lite" not in m and "preview" not in m]
        fast = [m for m in available if "lite" in m]
        return (strong or available)[0], (fast or available)[0]
    if provider == "OpenRouter":
        trusted = [m for m in available if openrouter_rank(m) < len(TRUSTED_OPENROUTER)] or available
        return trusted[0], min(trusted, key=model_size_b)
    return available[0], available[0]


def build_model(provider: str, model_id: str, api_key: str, ollama_host: str = "http://localhost:11434"):
    """La cle est passee directement au modele : jamais ecrite dans os.environ (pas de fuite entre utilisateurs)."""
    if provider == "Gemini":
        return Gemini(id=model_id, api_key=api_key, retries=1, delay_between_retries=3, exponential_backoff=True)
    if provider == "OpenRouter":
        # Pas de nouvel essai : sur les modeles gratuits, chaque essai consomme le quota du jour
        return OpenRouter(id=model_id, api_key=api_key, retries=0)
    if provider == "Ollama":
        return Ollama(id=model_id, host=ollama_host, options={"num_predict": 1024}, retries=0)
    return OpenAIChat(id=model_id, api_key=api_key, retries=2, delay_between_retries=3, exponential_backoff=True)


def explain_error(error: str) -> str:
    """Transforme une erreur technique en conseil clair pour l'utilisateur."""
    e = error.lower()
    if "free-models-per-day" in e:
        return "Limite gratuite du jour atteinte sur OpenRouter (pour tous les modeles gratuits). Utilise Gemini, attends demain, ou ajoute 10 $ de credit pour 1000 requetes/jour."
    if "401" in e or "incorrect api key" in e or "api key not valid" in e or "invalid api key" in e:
        return "Cle API refusee. Verifie qu'elle correspond bien au fournisseur choisi."
    if "no longer available" in e or "not found" in e or "404" in e:
        return "Ce modele n'est pas disponible avec ta cle. Choisis un autre modele."
    if "503" in e or "high demand" in e or "overloaded" in e or "unavailable" in e:
        return "Le fournisseur est surcharge en ce moment. Reessaie dans quelques minutes ou choisis un modele lite."
    if "429" in e or "rate limit" in e or "quota" in e:
        return "Limite de requetes atteinte. Attends un peu ou change de fournisseur."
    if "credit" in e or "insufficient" in e:
        return "Plus de credit sur ce compte. Change de fournisseur ou recharge le compte."
    return "Erreur inattendue. Reessaie ou change de modele."
