import json
import time
import traceback
from datetime import datetime, timezone
from typing import Literal, Optional

import instaloader
from fastapi import FastAPI, HTTPException, Query

from instaloader.exceptions import (
    BadCredentialsException,
    ConnectionException,
    LoginRequiredException,
    PrivateProfileNotFollowedException,
    QueryReturnedBadRequestException,
    TwoFactorAuthRequiredException,
)

app = FastAPI(title="Instagram Miner API", version="1.1.0")

# Modos de ordenação suportados
SortMode = Literal["recent", "most_liked", "most_commented", "best_engagement"]


def http_error(status_code: int, msg: str, e: Exception, debug: bool):
    """
    Padroniza o retorno de erro.
    - Sempre retorna: message, error_type, error
    - Se debug=true, também retorna stack (para diagnóstico)
    """
    detail = {
        "message": msg,
        "error_type": type(e).__name__,
        "error": str(e),
    }
    if debug:
        detail["stack"] = traceback.format_exc()
    raise HTTPException(status_code=status_code, detail=detail)


def build_loader(session_user: Optional[str] = None, debug: bool = False) -> instaloader.Instaloader:
    """
    Inicializa o Instaloader configurado para não baixar mídia (só metadados).
    Se session_user for informado, tenta carregar uma sessão salva para reduzir bloqueios (403/429).
    """
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    if session_user:
        try:
            L.load_session_from_file(session_user)
        except Exception as e:
            http_error(400, f"Falha ao carregar sessão '{session_user}'.", e, debug)

    return L


def mine_profile(
    username: str,
    max_posts: int = 30,
    sleep_s: float = 2.0,
    sort: SortMode = "recent",
    include_caption: bool = True,
    caption_max_len: int = 2000,
    session_user: Optional[str] = None,
    debug: bool = False,
    # ✅ demanda do chefe (rate/performance)
    min_rate: Optional[float] = None,
    top_rate: Optional[int] = None,
) -> dict:
    """
    Coleta dados do perfil + posts e monta um JSON com:
    - perfil
    - parâmetros usados
    - data da coleta
    - lista de posts (com engagement_rate calculado)
    Opcionalmente filtra por rate (engagement_rate).
    """
    username = username.lstrip("@").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username é obrigatório")

    L = build_loader(session_user=session_user, debug=debug)

    # Carrega o perfil (aqui acontecem boa parte dos 403/429)
    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except (LoginRequiredException, BadCredentialsException, TwoFactorAuthRequiredException) as e:
        http_error(401, f"Sessão inválida ou login necessário para acessar '{username}'.", e, debug)
    except PrivateProfileNotFollowedException as e:
        http_error(403, f"Perfil '{username}' é privado e a sessão atual não segue o perfil.", e, debug)
    except (ConnectionException, QueryReturnedBadRequestException) as e:
        http_error(502, f"Falha de conexão/consulta ao Instagram ao carregar '{username}'.", e, debug)
    except Exception as e:
        http_error(502, f"Erro inesperado ao carregar perfil '{username}'.", e, debug)

    # Coleta posts (limitado por max_posts) e respeita sleep_s entre iterações
    posts = []
    try:
        for i, post in enumerate(profile.get_posts(), start=1):
            if i > max_posts:
                break

            caption = post.caption or ""
            if not include_caption:
                caption = ""
            else:
                caption = caption[:caption_max_len]

            posts.append(
                {
                    "shortcode": post.shortcode,
                    "url": f"https://www.instagram.com/p/{post.shortcode}/",
                    "date_utc": post.date_utc.replace(tzinfo=timezone.utc).isoformat(),
                    "likes": int(post.likes),
                    "comments": int(post.comments),
                    "caption": caption,
                    "typename": post.typename,  # GraphImage / GraphVideo / GraphSidecar
                    "is_video": bool(post.is_video),
                }
            )

            time.sleep(max(0.0, sleep_s))

    except (LoginRequiredException,) as e:
        http_error(401, f"Login necessário durante a varredura de posts de '{username}'.", e, debug)
    except PrivateProfileNotFollowedException as e:
        http_error(403, f"Perfil '{username}' é privado e não pode ser varrido pela sessão atual.", e, debug)
    except (ConnectionException, QueryReturnedBadRequestException) as e:
        http_error(502, f"Falha do Instagram durante varredura de posts de '{username}'.", e, debug)
    except Exception as e:
        http_error(502, f"Erro inesperado ao varrer posts de '{username}'.", e, debug)

    # Calcula o rate (engagement_rate) usando followers atuais
    followers = profile.followers or 0
    for p in posts:
        if followers > 0:
            p["engagement_rate"] = (p["likes"] + p["comments"]) / followers
        else:
            p["engagement_rate"] = None

    # Ordenação (comportamento padrão do endpoint)
    if sort == "recent":
        posts.sort(key=lambda x: x["date_utc"], reverse=True)
    elif sort == "most_liked":
        posts.sort(key=lambda x: x["likes"], reverse=True)
    elif sort == "most_commented":
        posts.sort(key=lambda x: x["comments"], reverse=True)
    elif sort == "best_engagement":
        posts.sort(key=lambda x: (x["engagement_rate"] is not None, x["engagement_rate"]), reverse=True)

    # ✅ Filtro por "rate" (pedido do chefe)
    # Observação: a biblioteca não filtra na origem; o filtro acontece no endpoint após calcular engagement_rate.
    if (min_rate is not None) or (top_rate is not None):
        # garante ordenação por rate para aplicar top_rate corretamente
        posts_by_rate = sorted(
            posts,
            key=lambda x: (x["engagement_rate"] is not None, x["engagement_rate"]),
            reverse=True,
        )

        if min_rate is not None:
            posts_by_rate = [
                p for p in posts_by_rate
                if p["engagement_rate"] is not None and p["engagement_rate"] >= min_rate
            ]

        if top_rate is not None:
            posts_by_rate = posts_by_rate[:top_rate]

        posts = posts_by_rate

    out = {
        "profile": {
            "username": profile.username,
            "full_name": profile.full_name,
            "biography": profile.biography,
            "external_url": profile.external_url,
            "followers": int(profile.followers),
            "followees": int(profile.followees),
            "mediacount": int(profile.mediacount),
            "is_verified": bool(profile.is_verified),
            "is_private": bool(profile.is_private),
        },
        "params": {
            "max_posts": max_posts,
            "sleep_s": sleep_s,
            "sort": sort,
            "include_caption": include_caption,
            "caption_max_len": caption_max_len,
            "session_user": session_user,
            "debug": debug,
            "min_rate": min_rate,
            "top_rate": top_rate,
        },
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "posts": posts,
    }
    return out


@app.get("/health")
def health():
    """Healthcheck simples."""
    return {"ok": True}


@app.get("/v1/instagram/profile")
def api_profile(
    username: str = Query(..., description="Perfil do Instagram. Pode vir com ou sem @"),
    max_posts: int = Query(30, ge=1, le=200, description="Quantos posts coletar (limite de segurança)"),
    sleep_s: float = Query(2.0, ge=0.0, le=10.0, description="Delay entre posts para reduzir bloqueio"),
    sort: SortMode = Query("recent", description="Ordenação final dos posts"),
    include_caption: bool = Query(True, description="Incluir legenda no retorno"),
    caption_max_len: int = Query(2000, ge=0, le=10000, description="Limite de caracteres da legenda"),
    session_user: Optional[str] = Query(None, description="Usuário cuja sessão foi salva (load_session_from_file)"),
    debug: bool = Query(False, description="Se true, retorna stacktrace e detalhes completos do erro"),
    # ✅ demanda do chefe (rate/performance)
    min_rate: Optional[float] = Query(None, ge=0.0, le=1.0, description="Filtra posts com engagement_rate >= min_rate"),
    top_rate: Optional[int] = Query(None, ge=1, le=200, description="Retorna apenas os top N posts por engagement_rate"),
):
    """
    Endpoint principal.
    - Retorna dados do perfil e posts
    - Calcula engagement_rate por post
    - Permite filtrar posts por rate via min_rate/top_rate
    - Permite debug=true para detalhar erros
    """
    return mine_profile(
        username=username,
        max_posts=max_posts,
        sleep_s=sleep_s,
        sort=sort,
        include_caption=include_caption,
        caption_max_len=caption_max_len,
        session_user=session_user,
        debug=debug,
        min_rate=min_rate,
        top_rate=top_rate,
    )
