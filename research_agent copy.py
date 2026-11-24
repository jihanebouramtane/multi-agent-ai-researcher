# Import the required libraries
import streamlit as st
from agno.agent import Agent
from agno.team import Team

# [AMELIORATION] Imports ajoutes pour les ameliorations (voir les sections marquees [AMELIORATION n])
import asyncio
import os
import re
import time

from agno.models.fallback import FallbackConfig
from dotenv import load_dotenv

from researcher.providers import PROVIDERS, build_model, default_roles, explain_error, list_models
from researcher.security import SECURITY_INSTRUCTIONS, RateLimiter, get_secret, validate_query
from researcher.tools import (
    get_top_hackernews_stories,
    read_article,
    search_arxiv_papers,
    search_github_repositories,
    search_hackernews,
    search_web,
    search_wikipedia,
)

load_dotenv()

# Set up the Streamlit app
st.title("Multi-Agent AI Researcher 🔍🤖")
st.caption("This app allows you to research top stories and users on HackerNews and write blogs, reports and social posts.")


# [AMELIORATION 1] Plusieurs fournisseurs de LLM au lieu d'OpenAI seul.
# Original : une cle OpenAI saisie puis ecrite dans os.environ["OPENAI_API_KEY"].
# Maintenant : Gemini, OpenRouter, OpenAI ou Ollama ; la cle peut venir du fichier .env (jamais affichee)
# et elle est passee directement au modele, sans etre ecrite dans os.environ (pas de fuite entre utilisateurs).
@st.cache_data(show_spinner=False, ttl=3600)
def cached_models(provider: str, api_key: str, ollama_host: str) -> list[str]:
    return list_models(provider, api_key, ollama_host)


with st.sidebar:
    st.header("⚙️ Configuration")
    provider = st.radio("Fournisseur du LLM", list(PROVIDERS), captions=[p["caption"] for p in PROVIDERS.values()])
    conf = PROVIDERS[provider]
    api_key, ollama_host = "", "http://localhost:11434"
    if conf["secret"]:
        api_key = get_secret(conf["secret"])
        if api_key:
            st.success("Cle chargee depuis la configuration du serveur", icon="🔒")
        else:
            api_key = st.text_input(conf["key_label"], type="password", help=conf["help"]).strip()
    else:
        ollama_host = st.text_input("Adresse Ollama", ollama_host)

if provider == "OpenAI" and api_key.startswith("sk-or-"):
    st.warning("Cette cle commence par sk-or- : c'est une cle OpenRouter. Choisis OpenRouter a gauche.")
    st.stop()

if api_key or provider == "Ollama":
    # [AMELIORATION 2] Detection automatique des modeles disponibles avec la cle de l'utilisateur
    try:
        available = cached_models(provider, api_key, ollama_host)
    except Exception as e:
        st.error(f"Impossible de lire la liste des modeles : {explain_error(str(e))}")
        st.stop()
    available = [m for m in available if m not in st.session_state.get("unavailable", set())]
    if not available:
        st.error("Aucun modele disponible avec ce fournisseur.")
        st.stop()

    # [AMELIORATION 3] Routage des modeles par role.
    # Original : gpt-4o-mini partout. Maintenant : un modele puissant pour le chef d'equipe (il planifie
    # et redige) et un modele rapide pour les agents (ils appellent un outil et resument).
    leader_default, member_default = default_roles(provider, available)
    with st.sidebar:
        if st.toggle("Un modele par role (recommande)", value=True):
            leader_id = st.selectbox("🧭 Modele du chef d'equipe", available, index=available.index(leader_default))
            member_id = st.selectbox("🔧 Modele des agents", available, index=available.index(member_default))
        else:
            leader_id = member_id = st.selectbox("Modele unique", available)
        extra_fallbacks = []
        if provider != "Ollama" and st.checkbox("Ollama local en dernier recours"):
            try:
                extra_fallbacks.append(("Ollama", cached_models("Ollama", "", ollama_host)[0]))
            except Exception:
                st.caption("Ollama n'est pas lance sur ce PC.")
        st.divider()
        use_cache = st.toggle("Reutiliser les rapports recents (1 h)", value=True)
        debug = st.toggle("Journal detaille dans le terminal", value=False)

    # [AMELIORATION 4] Modeles de secours (fallback) : si un modele est surcharge (503) ou limite (429),
    # l'agent bascule automatiquement sur un autre modele du meme fournisseur, puis sur Ollama si active.
    fallback_events: list[str] = []

    def model(model_id: str):
        return build_model(provider, model_id, api_key, ollama_host)

    def fallback_config(model_id: str):
        chain = [(provider, m) for m in available if m != model_id][:3] + extra_fallbacks
        return FallbackConfig(on_error=[build_model(p, m, api_key, ollama_host) for p, m in chain],
                              callback=lambda p, f, e: fallback_events.append(f"{p} indisponible -> bascule sur {f}"))

    # [AMELIORATION 5] Securite et cout, appliques a chaque agent :
    # - le contenu web est traite comme une donnee, jamais comme une instruction (anti prompt-injection)
    # - nombre maximum d'appels d'outils par agent (un agent ne peut pas boucler et faire exploser la facture)
    member_settings = dict(
        instructions=SECURITY_INSTRUCTIONS + ["Return compact results: titles, key facts and URLs only."],
        tool_call_limit=4,
        add_datetime_to_context=True,
    )

    hn_researcher = Agent(
        name="HackerNews Researcher",
        model=model(member_id),
        fallback_config=fallback_config(member_id),
        role="Gets top stories from hackernews. Uses search_hackernews to find stories about a topic.",
        # [AMELIORATION 6] Original : HackerNewsTools (top stories uniquement, reponses tres longues).
        # Maintenant : recherche par mot-cle dans toute l'archive + resultats compacts (beaucoup moins de tokens).
        tools=[search_hackernews, get_top_hackernews_stories],
        **member_settings,
    )

    web_searcher = Agent(
        name="Web Searcher",
        model=model(member_id),
        fallback_config=fallback_config(member_id),
        role="Searches the web for information on a topic",
        # [AMELIORATION 7] Wikipedia en secours quand la recherche web (DuckDuckGo) echoue
        tools=[search_web, search_wikipedia],
        **member_settings,
    )

    article_reader = Agent(
        name="Article Reader",
        model=model(member_id),
        fallback_config=fallback_config(member_id),
        role="Reads articles from URLs.",
        # [AMELIORATION 8] Lecteur d'articles protege contre le SSRF (refuse localhost et le reseau interne,
        # y compris via redirection) et texte limite a 4000 caracteres.
        tools=[read_article],
        **member_settings,
    )

    # [AMELIORATION 9] Nouvel agent : projets open source populaires (GitHub) et articles de recherche (arXiv)
    tech_scout = Agent(
        name="Tech Scout",
        model=model(member_id),
        fallback_config=fallback_config(member_id),
        role="Finds popular open-source projects on GitHub and recent research papers on arXiv.",
        tools=[search_github_repositories, search_arxiv_papers],
        **member_settings,
    )

    hackernews_team = Team(
        name="HackerNews Team",
        model=model(leader_id),
        fallback_config=fallback_config(leader_id),
        members=[hn_researcher, web_searcher, article_reader, tech_scout],
        # [AMELIORATION 10] Instructions revues : recherches en parallele, pas de tache en double,
        # rapport structure et dans la langue de l'utilisateur.
        # Original : HackerNews, puis lecture, puis web, l'un apres l'autre.
        instructions=[
            "First, in ONE turn, delegate in parallel: the HackerNews Researcher (keyword search, 5 to 8 relevant stories), "
            "the Tech Scout (popular GitHub repositories and recent arXiv papers) and the Web Searcher (short context).",
            "Then, ask the article reader to read the 2 or 3 most relevant links.",
            "Important: you must provide the article reader with the links to read.",
            "Never delegate the same task twice.",
            "Finally, provide a thoughtful and engaging summary: a title, 3 to 6 key trends backed by evidence, "
            "notable projects and papers, and a Sources list with links. Keep it under 700 words.",
            "Answer in the same language as the user's question.",
        ] + SECURITY_INSTRUCTIONS,
        tool_call_limit=8,
        add_datetime_to_context=True,
        markdown=True,
        debug_mode=debug,
    )

    # [AMELIORATION 11] Progression des agents affichee en direct.
    # Original : hackernews_team.run(query, stream=False) -> page vide jusqu'a la fin.
    # Maintenant : streaming asynchrone (les agents delegues en meme temps travaillent en parallele).
    labels = {"hackernews-researcher": "HackerNews Researcher", "web-searcher": "Web Searcher",
              "article-reader": "Article Reader", "tech-scout": "Tech Scout"}

    async def run_team(query: str, status, answer_box) -> dict:
        answer, error, steps, tokens = "", None, [], 0
        async for ev in hackernews_team.arun(query, stream=True, stream_events=True):
            name, tool = getattr(ev, "event", ""), getattr(ev, "tool", None)
            if name == "TeamToolCallStarted" and tool is not None:
                args = tool.tool_args or {}
                who = labels.get(args.get("member_id"), args.get("member_id", tool.tool_name))
                steps.append(f"🧭 Le chef d'equipe confie une tache a **{who}** : {str(args.get('task', ''))[:150]}")
                status.write(steps[-1])
                answer = ""  # le texte ecrit avant une delegation n'est pas la reponse finale
                answer_box.empty()
            elif name == "ToolCallStarted" and tool is not None:
                args = ", ".join(f"{k}={v}" for k, v in (tool.tool_args or {}).items())[:100]
                steps.append(f"🔧 {getattr(ev, 'agent_name', '') or 'Agent'} utilise `{tool.tool_name}` {args}")
                status.write(steps[-1])
            elif name == "TeamRunContent" and getattr(ev, "content", None):
                answer += str(ev.content)
                answer_box.markdown(answer)
            elif name in ("TeamRunCompleted", "RunCompleted") and getattr(ev, "metrics", None):
                tokens += ev.metrics.total_tokens or 0
            elif name == "TeamRunError":
                error = getattr(ev, "content", None) or "Erreur inconnue"
        return {"report": answer, "error": error, "steps": steps, "tokens": tokens}

    # Input field for the report query
    # [AMELIORATION 12] Formulaire avec bouton : la recherche ne se relance plus a chaque clic dans les reglages.
    with st.form("search"):
        query = st.text_input("Enter your report query", max_chars=500)
        submitted = st.form_submit_button("🚀 Lancer la recherche", type="primary")

    if submitted:
        # [AMELIORATION 13] Validation de la question et limite de 5 recherches / 10 min par utilisateur
        limiter = RateLimiter(st.session_state, max_runs=int(os.environ.get("MAX_RUNS_PER_10_MIN", "5")))
        try:
            query = validate_query(query)
        except ValueError as e:
            st.warning(str(e))
            st.stop()
        allowed, wait = limiter.check()
        if not allowed:
            st.warning(f"Limite de recherches atteinte. Reessaie dans {wait // 60 + 1} min.")
            st.stop()

        # [AMELIORATION 14] Cache des rapports : une question deja posee dans l'heure repond instantanement
        cache = st.session_state.setdefault("report_cache", {})
        key = (query.lower(), provider, leader_id, member_id)
        if use_cache and key in cache and time.time() - cache[key]["time"] < 3600:
            st.session_state["result"] = dict(cache[key], cached=True)
        else:
            limiter.record()
            live = st.empty()
            with live.container():
                status = st.status("Les agents travaillent...", expanded=True)
                answer_box = st.empty()
            start = time.time()
            # Get the response from the assistant
            try:
                result = asyncio.run(run_team(query, status, answer_box))
            except Exception as e:
                result = {"report": "", "error": str(e), "steps": [], "tokens": 0}
            live.empty()
            result.update(query=query, duration=int(time.time() - start), leader=leader_id, member=member_id, time=time.time())
            for event in fallback_events:
                st.info(event)
            gone = re.search(r"models/([\w.\-]+) is no longer available", result["error"] or "")
            if gone:  # modele retire par le fournisseur : exclu pour la suite
                st.session_state.setdefault("unavailable", set()).add(gone.group(1))
            if result["report"] and not result["error"]:
                cache[key] = result
            st.session_state["result"] = result

    # [AMELIORATION 15] Resultat lisible : metriques, rapport, export Markdown, journal des agents, erreurs claires
    result = st.session_state.get("result")
    if result:
        st.subheader(result["query"])
        if result["error"]:
            st.error(explain_error(result["error"]))
            with st.expander("Detail technique de l'erreur"):
                st.code(result["error"][:2000])
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Duree", "cache ⚡" if result.get("cached") else f"{result['duration']} s")
            c2.metric("Tokens", f"{result['tokens']:,}".replace(",", " ") if result["tokens"] else "-")
            c3.metric("Etapes des agents", len(result["steps"]))
            st.caption(f"🧭 Chef d'equipe : `{result['leader']}`  ·  🔧 Agents : `{result['member']}`")
            st.markdown(result["report"] or "_Les agents n'ont renvoye aucun texte. Essaie un autre modele._")
            if result["report"]:
                st.download_button("⬇️ Telecharger le rapport (Markdown)", result["report"], file_name="rapport.md")
            with st.expander(f"Journal des agents ({len(result['steps'])} etapes)"):
                for step in result["steps"]:
                    st.markdown(step)
else:
    st.info("Entre ta cle API dans la barre de gauche pour commencer.")
