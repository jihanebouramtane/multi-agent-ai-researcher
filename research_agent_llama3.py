# Multi-Agent AI Researcher - version amelioree
# Base : awesome-llm-apps / multi_agent_researcher
# Ameliorations : choix du fournisseur (Gemini gratuit, OpenAI ou Ollama en local),
# detection automatique des modeles disponibles, modeles de secours (fallback)
# cloud puis local, tentatives automatiques, affichage clair des erreurs.
import os
import re

import streamlit as st
from agno.agent import Agent
from agno.models.fallback import FallbackConfig
from agno.models.google import Gemini
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.run.base import RunStatus
from agno.team import Team
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.tools.hackernews import HackerNewsTools
from agno.tools.newspaper4k import Newspaper4kTools

MAX_FALLBACKS = 3
EXCLUDED = ("image", "tts", "audio", "live", "embedding", "vision", "robotics", "computer-use")


# ---------- Choix des modeles ----------

def sort_key(model_id: str):
    """Ordre de preference : stable avant preview, standard avant lite, version la plus recente d'abord."""
    numbers = [int(n) for n in re.findall(r"\d+", model_id)[:2]]
    version = tuple(numbers) if numbers else (0,)
    return ("preview" in model_id or "exp" in model_id, "lite" in model_id, tuple(-n for n in version))


@st.cache_data(show_spinner=False, ttl=3600)
def list_gemini_models(api_key: str) -> list[str]:
    """Demande a Google la liste des modeles Gemini Flash accessibles avec cette cle."""
    from google import genai

    client = genai.Client(api_key=api_key)
    ids = []
    for m in client.models.list():
        model_id = m.name.replace("models/", "")
        actions = getattr(m, "supported_actions", None) or []
        if "flash" not in model_id or any(word in model_id for word in EXCLUDED):
            continue
        if actions and "generateContent" not in actions:
            continue
        ids.append(model_id)
    return sorted(set(ids), key=sort_key)


@st.cache_data(show_spinner=False, ttl=30)
def list_ollama_models(host: str) -> list[str]:
    """Liste les modeles installes sur Ollama (en local)."""
    import ollama

    return sorted(m.model for m in ollama.Client(host=host).list().models)


def build_model(provider: str, model_id: str, api_key: str):
    # retries=1 : une seule nouvelle tentative, puis on passe vite au modele de secours
    if provider == "Gemini":
        return Gemini(id=model_id, api_key=api_key, retries=1, delay_between_retries=3, exponential_backoff=True)
    if provider == "Ollama":
        # max_tokens n'existe plus dans Agno : on passe par options/num_predict
        return Ollama(id=model_id, host=ollama_host, options={"num_predict": 1024}, retries=1)
    return OpenAIChat(id=model_id, api_key=api_key, retries=2, delay_between_retries=3, exponential_backoff=True)


# ---------- Interface ----------

st.title("Multi-Agent AI Researcher 🔍🤖")
st.caption("Recherche sur HackerNews et le web avec une equipe d'agents IA.")

with st.sidebar:
    st.header("Configuration")
    provider = st.radio(
        "Fournisseur du LLM",
        ["Gemini", "OpenAI", "Ollama"],
        captions=["Gratuit (cle Google)", "Payant", "Local, gratuit, sans cle"],
    )
    api_key = ""
    if provider == "Gemini":
        api_key = st.text_input("Google Gemini API Key", type="password", help="https://aistudio.google.com/apikey")
    elif provider == "OpenAI":
        api_key = st.text_input("OpenAI API Key", type="password")
    ollama_host = st.text_input("Adresse Ollama", "http://localhost:11434") if provider == "Ollama" else "http://localhost:11434"

if provider != "Ollama" and not api_key:
    st.info("Entre ta cle API dans la barre de gauche pour commencer.")
    st.stop()


def local_models() -> list[str]:
    try:
        return list_ollama_models(ollama_host)
    except Exception:
        return []


# Liste des modeles disponibles
if provider == "Gemini":
    try:
        available = list_gemini_models(api_key)
    except Exception as e:
        st.error(f"Impossible de lire la liste des modeles Gemini (cle invalide ?) : {e}")
        st.stop()
    if not available:
        st.error("Aucun modele Gemini Flash disponible avec cette cle.")
        st.stop()
elif provider == "OpenAI":
    available = ["gpt-4o-mini", "gpt-4o"]
else:
    available = local_models()
    if not available:
        st.error("Ollama ne repond pas ou aucun modele n'est installe.")
        st.caption("Installe Ollama (https://ollama.com), puis lance : ollama pull llama3.2")
        st.stop()

with st.sidebar:
    primary = st.selectbox("Modele principal", available)
    fallbacks = [(provider, m) for m in available if m != primary][:MAX_FALLBACKS]
    # Dernier recours : un modele local Ollama si le cloud est entierement indisponible
    if provider != "Ollama" and st.checkbox("Ollama local en dernier recours", help="Utile si tous les modeles cloud sont surcharges."):
        local = local_models()
        if local:
            fallbacks.append(("Ollama", local[0]))
        else:
            st.caption("Ollama n'est pas lance sur ce PC.")
    st.caption("Modeles de secours : " + (", ".join(m for _, m in fallbacks) or "aucun"))

# Journal des basculements vers un modele de secours, affiche apres la reponse
fallback_events: list[str] = []


def on_fallback(primary_id: str, fallback_id: str, error: Exception) -> None:
    fallback_events.append(f"{primary_id} indisponible -> bascule sur {fallback_id}")


def model():
    return build_model(provider, primary, api_key)


def fallback_config():
    return FallbackConfig(on_error=[build_model(p, m, api_key) for p, m in fallbacks], callback=on_fallback)


# ---------- Equipe d'agents ----------

hn_researcher = Agent(
    name="HackerNews Researcher",
    model=model(),
    fallback_config=fallback_config(),
    role="Gets top stories from hackernews.",
    tools=[HackerNewsTools()],
)

web_searcher = Agent(
    name="Web Searcher",
    model=model(),
    fallback_config=fallback_config(),
    role="Searches the web for information on a topic",
    tools=[DuckDuckGoTools()],
    add_datetime_to_context=True,
)

article_reader = Agent(
    name="Article Reader",
    model=model(),
    fallback_config=fallback_config(),
    role="Reads articles from URLs.",
    tools=[Newspaper4kTools()],
)

hackernews_team = Team(
    name="HackerNews Team",
    model=model(),
    fallback_config=fallback_config(),
    members=[hn_researcher, web_searcher, article_reader],
    instructions=[
        "First, search hackernews for what the user is asking about.",
        "Then, ask the article reader to read the links for the stories to get more information.",
        "Important: you must provide the article reader with the links to read.",
        "Then, ask the web searcher to search for each story to get more information.",
        "Finally, provide a thoughtful and engaging summary.",
        "Answer in the same language as the user's question.",
    ],
    markdown=True,
    debug_mode=True,
    show_members_responses=True,
)

query = st.text_input("Enter your report query")

if query:
    with st.spinner("Les agents travaillent... (30 a 90 secondes)"):
        try:
            response = hackernews_team.run(query, stream=False)
        except Exception as e:
            response = None
            st.error(f"Erreur : {e}")

    for event in fallback_events:
        st.info(event)

    if response is not None:
        if response.status == RunStatus.error:
            st.error(f"Erreur de l'API apres tous les essais : {response.content}")
            st.caption("Google ou OpenAI est peut-etre surcharge. Reessaie dans quelques minutes ou choisis un autre modele.")
        else:
            st.markdown(response.content)